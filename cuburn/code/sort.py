
import numpy as np
import pycuda.driver as cuda
import pycuda.compiler
import tempita

_CODE = tempita.Template(r"""
#include <cuda.h>
#include <stdio.h>

#define GRP_RDX_FACTOR (GRPSZ / RDXSZ)
#define GRP_BLK_FACTOR (GRPSZ / BLKSZ)
#define GRPSZ {{group_size}}
#define RBITS {{radix_bits}}
#define RDXSZ {{radix_size}}
#define BLKSZ 512

#define get_radix(r, k, l) \
        asm("bfe.u32 %0, %1, %2, {{radix_bits}};" : "=r"(r) : "r"(k), "r"(l))

// TODO: experiment with different block / group sizes
__global__
void prefix_scan(
        int *offsets,
        int *pfxs,
        const unsigned int *keys,
        const int lo_bit
) {
    const int tid = threadIdx.x;
    __shared__ int shr_pfxs[RDXSZ];

{{if radix_size <= 512}}
    if (tid < RDXSZ) shr_pfxs[tid] = 0;
{{else}}
{{for i in range(0, radix_size, 512)}}
    shr_pfxs[tid+{{i}}] = 0;
{{endfor}}
{{endif}}

    __syncthreads();
    int idx = tid + GRPSZ * blockIdx.x;

    for (int i = 0; i < GRP_BLK_FACTOR; i++) {
        // TODO: load 2 at once, compute, use a BFI to pack the two offsets
        //       into an int to halve storage / bandwidth
        // TODO: separate or integrated loop vars? unrolling?
        int key = keys[idx];
        int radix;
        get_radix(radix, key, lo_bit);
        offsets[idx] = atomicAdd(shr_pfxs + radix, 1);
        idx += BLKSZ;
    }

    __syncthreads();

{{if radix_size <= 512}}
    if (tid < RDXSZ) pfxs[tid + RDXSZ * blockIdx.x] = shr_pfxs[tid];
{{else}}
{{for i in range(0, radix_size, 512)}}
    pfxs[tid + {{i}} + RDXSZ * blockIdx.x] = shr_pfxs[tid + {{i}}];
{{endfor}}
{{endif}}

}

// Calculate group-local exclusive prefix sums (the number of keys in the
// current group with a strictly smaller radix). Must be launched in a
// (32,1,1) block, regardless of block or radix size.
__global__
void calc_local_pfxs(
        int *locals,
        const int *pfxs
) {
    const int tid = threadIdx.x;
    const int tid5 = tid << 5;
    __shared__ int swap[32*32];

    int base = RDXSZ * 32 * blockIdx.x;

    int value = 0;

    // The contents of 32 group radix counts are loaded in 32-element chunks
    // into shared memory, rotated by 1 unit each group to avoid bank
    // conflicts. Each thread in the warp sums across each group serially,
    // updating the values as it goes, then the results are written coherently
    // to global memory.
    //
    // This leaves the SM underloaded, as this only allows 12 warps per SM. It
    // might be better to halve the chunk size and lose some coalescing
    // efficiency; need to benchmark. It's a relatively cheap step, though.

    for (int j = 0; j < RDXSZ / 32; j++) {
        int jj = j << 5;
        for (int i = 0; i < 32; i++) {
            int base_offset = (i << RBITS) + jj + base + tid;
            int swap_offset = (i << 5) + ((i + tid) & 0x1f);
            swap[swap_offset] = pfxs[base_offset];
        }

#pragma unroll
        for (int i = 0; i < 32; i++) {
            int swap_offset = tid5 + ((i + tid) & 0x1f);
            int tmp = swap[swap_offset];
            swap[swap_offset] = value;
            value += tmp;
        }

        for (int i = 0; i < 32; i++) {
            int base_offset = (i << RBITS) + jj + base + tid;
            int swap_offset = (i << 5) + ((i + tid) & 0x1f);
            locals[base_offset] = swap[swap_offset];
        }
    }
}

// All three prefix_sum functions must be called with a block of (RDXSZ, 1, 1).

// Take the prefix scans generated in the first pass and sum them
// vertically (by radix value), sharded into horizontal groups. Store the
// sums by shard and radix in 'condensed'.
__global__
void prefix_sum_condense(
        int *condensed,
        const int *pfxs,
        const int ngrps,
        const int grpwidth
) {
    const int tid = threadIdx.x;
    int sum = 0;

    int idx = grpwidth * blockIdx.x * RDXSZ + tid;
    int maxidx = min(grpwidth * (blockIdx.x + 1), ngrps) * RDXSZ;

    for (; idx < maxidx; idx += RDXSZ) sum += pfxs[idx];

    condensed[blockIdx.x * RDXSZ + tid] = sum;
}

// Sum the partially-condensed sums completely. Scan the sums horizontally.
// Distribute the scanned sums back to the partially-condensed sums.
__global__
void prefix_sum_inner(
        int *glob_pfxs,
        int *condensed,     // input and output
        const int ncondensed
) {
    const int tid = threadIdx.x;
    int sum = 0;
    int idx = tid;
    __shared__ int sums[RDXSZ];

    for (int i = 0; i < ncondensed; i++) {
        sum += condensed[idx];
        idx += RDXSZ;
    }

    // Yeah, the entire device will be stalled on this horribly ineffecient
    // computation, but it only happens once per sort
    sums[tid] = sum;
    __syncthreads();
    sum = 0;

    // Intentionally exclusive indexing here
    for (int i = 0; i < tid; i++) sum += sums[i];
    __syncthreads();

    sums[tid] = glob_pfxs[tid] = sum;
    idx = tid;

    for (int i = 0; i < ncondensed; i++) {
        int c = condensed[idx];
        condensed[idx] = sum;
        sum += c;
        idx += RDXSZ;
    }
}

// Distribute the partially-condensed sums back to the uncondensed sums.
__global__
void prefix_sum_distribute(
        int *pfxs,          // input and output
        const int *condensed,
        const int ngrps,
        const int grpwidth
) {
    const int tid = threadIdx.x;
    int sum = condensed[blockIdx.x * RDXSZ + tid];

    int idx = grpwidth * blockIdx.x * RDXSZ + tid;
    int maxidx = min(grpwidth * (blockIdx.x + 1), ngrps) * RDXSZ;

    for (; idx < maxidx; idx += RDXSZ) {
        int p = pfxs[idx];
        pfxs[idx] = sum;
        sum += p;
    }
}

__global__
void radix_sort_direct(
        int *sorted_keys,
        const int *keys,
        const int *offsets,
        const int *pfxs
) {
    const int tid = threadIdx.x;
    const int blk_offset = GRPSZ * blockIdx.x;

    int i = tid;
    for (int j = 0; j < GRP_BLK_FACTOR; j++) {
        int value = keys[i+blk_offset];
        int offset = offsets[i+blk_offset];
        sorted_keys[offset] = value;
        i += BLKSZ;
    }
}

#undef BLKSZ
#define BLKSZ {{group_size / 8}}
__global__
void radix_sort(
        int *sorted_keys,
        const int *keys,
        const int *offsets,
        const int *pfxs,
        const int *locals,
        const int lo_bit
) {
    const int tid = threadIdx.x;
    const int blk_offset = GRPSZ * blockIdx.x;
    __shared__ int shr_offs[RDXSZ];
    __shared__ int defer[GRPSZ];

    const int pfx_i = RDXSZ * blockIdx.x + tid;
    if (tid < RDXSZ) shr_offs[tid] = locals[pfx_i];
    __syncthreads();

    for (int i = tid; i < GRPSZ; i += BLKSZ) {
        int key = keys[i+blk_offset];
        int radix;
        get_radix(radix, key, lo_bit);
        int offset = offsets[i+blk_offset] + shr_offs[radix];
        defer[offset] = key;
    }
    __syncthreads();

    if (tid < RDXSZ) shr_offs[tid] = pfxs[pfx_i] - shr_offs[tid];
    __syncthreads();

    int i = tid;
#pragma unroll
    for (int j = 0; j < GRP_BLK_FACTOR; j++) {
        int key = defer[i];
        int radix;
        get_radix(radix, key, lo_bit);
        int offset = shr_offs[radix] + i;
        sorted_keys[offset] = key;
        i += BLKSZ;
    }
}
""")

class Sorter(object):
    mod = None
    group_size = 8192
    radix_bits = 8

    @classmethod
    def init_mod(cls):
        if cls.__dict__.get('mod') is None:
            cls.radix_size = 1 << cls.radix_bits
            code = _CODE.substitute(group_size=cls.group_size,
                    radix_bits=cls.radix_bits, radix_size=cls.radix_size)
            cubin = pycuda.compiler.compile(code)
            cls.mod = cuda.module_from_buffer(cubin)
            with open('/tmp/sort_kern.cubin', 'wb') as fp:
                fp.write(cubin)
            for name in ['prefix_scan', 'prefix_sum_condense',
                         'prefix_sum_inner', 'prefix_sum_distribute']:
                f = cls.mod.get_function(name)
                setattr(cls, name, f)
                f.set_cache_config(cuda.func_cache.PREFER_L1)
            cls.calc_local_pfxs = cls.mod.get_function('calc_local_pfxs')
            cls.radix_sort = cls.mod.get_function('radix_sort')

    def __init__(self, max_size):
        self.init_mod()
        self.max_size = max_size
        assert max_size % self.group_size == 0
        max_grids = max_size / self.group_size

        self.doffsets = cuda.mem_alloc(self.max_size * 4)
        self.dpfxs = cuda.mem_alloc(max_grids * self.radix_size * 4)
        self.dlocals = cuda.mem_alloc(max_grids * self.radix_size * 4)

        # There are probably better ways to choose how many condensation
        # groups to launch. TODO: maybe pick one if I care
        self.ncond = 32
        self.dcond = cuda.mem_alloc(self.radix_size * self.ncond * 4)
        self.dglobal = cuda.mem_alloc(self.radix_size * 4)

    def sort(self, dst, src, size, lo_bit=0, stream=None):
        """
        Sort 'src' by the bits from lo_bit+radix_bits to lo_bit, where 0 is
        the LSB. Store the result in 'dst'.

        Note that this is *not* a stable sort! It won't jumble your data
        haphazardly, but one- or two-position swaps are very common. This will
        hopefully be resolved soon, but until then, it is unsuitable for
        implementing larger sorts from multiple passes of this sort.
        """

        assert size <= self.max_size and size % self.group_size == 0
        grids = size / self.group_size

        self.prefix_scan(self.doffsets, self.dpfxs, src, np.int32(lo_bit),
            block=(512, 1, 1), grid=(grids, 1), stream=stream)

        self.calc_local_pfxs(self.dlocals, self.dpfxs,
            block=(32, 1, 1), grid=(grids / 32, 1), stream=stream)

        ngrps = np.int32(grids)
        grpwidth = np.int32(np.ceil(float(grids) / self.ncond))

        self.prefix_sum_condense(self.dcond, self.dpfxs, ngrps, grpwidth,
            block=(self.radix_size, 1, 1), grid=(self.ncond, 1), stream=stream)
        self.prefix_sum_inner(self.dglobal, self.dcond, np.int32(self.ncond),
            block=(self.radix_size, 1, 1), grid=(1, 1), stream=stream)
        self.prefix_sum_distribute(self.dpfxs, self.dcond, ngrps, grpwidth,
            block=(self.radix_size, 1, 1), grid=(self.ncond, 1), stream=stream)

        self.radix_sort(dst, src,
            self.doffsets, self.dpfxs, self.dlocals, np.int32(lo_bit),
            block=(self.group_size / 8, 1, 1), grid=(grids, 1), stream=stream)

    @classmethod
    def test(cls, count, correctness=False):
        keys = np.uint32(np.random.randint(0, 1<<cls.radix_bits, size=count))
        dkeys = cuda.to_device(keys)
        dout = cuda.mem_alloc(count * 4)

        sorter = cls(count)
        stream = cuda.Stream()

        def test_stub(shift):
            for i in range(10):
                evt_a = cuda.Event().record(stream)
                sorter.sort(dout, dkeys, count, shift, stream=stream)
                evt_b = cuda.Event().record(stream)
                evt_b.synchronize()
                dur = evt_b.time_since(evt_a) / 1000.

                print ( '  Overall time: %g secs'
                        '\t%g %d-bit keys/sec\t%g 32-bit keys/sec') % (
                        dur, count/dur, sorter.radix_bits,
                        count * sorter.radix_bits / (dur * 32) )

        print '\n\n%d bit sort' % cls.radix_bits
        print 'Testing speed'
        test_stub(0)

        if '-s' not in sys.argv:
            print '\nTesting correctness'
            out = cuda.from_device(dout, (count,), np.uint32)
            sort = np.sort(keys)
            if np.all(out == sort):
                print 'Correct'
            else:
                assert False, 'Oh no'

        print '\nTesting speed at shifts'
        for b in range(cls.radix_bits - 1):
            print 'Performance with %d sig bits' % (cls.radix_bits - b)
            test_stub(b)

if __name__ == "__main__":
    import sys
    import pycuda.autoinit

    np.set_printoptions(precision=5, edgeitems=20,
                        linewidth=100, threshold=90)
    count = 1 << 26

    np.random.seed(42)

    correct = '-s' not in sys.argv
    for g in (8192, 4096):
        print '\n\n== GROUP SIZE %d ==' % g
        Sorter.group_size = g
        for b in [7,8,9,10]:
            if g == 4096 and b == 10: continue
            Sorter.radix_bits = b
            Sorter.test(count, correct)
            del Sorter.mod
