# Workload model: video processing library

Design and review data-touching work with the `production-shape` skill: production shape,
cost test, naive shape, workload note, candidate walk, bounded consumer, snapshot then
deltas, query cover, operation lowering, single writer, steady state, match the defence to
the boundary, evolution level.

Root file. A library has no deployment of its own: callers set the scales, so the drivers
below are **parameters**, and now / target / ceiling read as typical caller / supported /
supported ceiling.

## Maturity

Production. Embedded in players, editors and transcoding services.

## Scale drivers

| Driver | Typical | Supported | Ceiling | Source |
|---|---|---|---|---|
| `frame.pixels` | 2.1M (1080p) | 8.3M (4K) | 33M (8K) | caller survey, 2026-08 |
| `fps` | 30 | 60 | 240 | same |
| `streams` (concurrent pipelines per process) | 1 | 8 | 64 (transcode servers) | same |
| `graph.ops` (operations per pipeline) | 4 | 12 | 40 | same |

## Shared entities

| Entity | Count per parent (mean / p50 / p99) | Size (mean / p99) | Distribution | Source | Retention |
|---|---|---|---|---|---|
| `frame` | per stream: `fps` per second | `frame.pixels` x 1.5 B (NV12) to 8 B (RGBA16F) | | caller | one pipeline pass |

## Usage

The public API calls that matter.

| Operation | Arrival | Rate | Work per operation | Latency |
|---|---|---|---|---|
| `build`: build or reconfigure a pipeline graph | closed, rare: per stream start, per resolution change | | plan `graph.ops` | < 50 ms (a reconfigure must not drop frames) |
| `process`: push a frame through a pipeline | closed: caller waits for completion or a free slot | `fps` x `streams` | `graph.ops` operations on one `frame` | within 1 / `fps` per stream, pipelined |

## Dependencies and budgets

| System | Used for | Budget | Availability |
|---|---|---|---|
| `cpu.simd` | kernels | ISA varies by platform: NEON, AVX2, AVX-512, or scalar only | detected at load |
| `gpu` | kernels, residency | `gpu.memory`; compute queues shared with the caller's rendering | may be absent |
| `host_device_link` | moving frames between CPU and GPU memory | PCIe x16 ~25 GB/s; unified memory: no copy | |
| `hw_codec` | hardware decode and encode | concurrent sessions are driver-limited, often single digits on consumer GPUs | sessions can be refused at runtime |

## Evolution

| Surface | Level | Window | Refinements |
|---|---|---|---|
| Public API | public contract | one major version | semver |
| Serialized pipeline graphs | mixed-version | | callers store and reload them across library versions |
| Kernel cache on disk | rebuildable | | keyed by library version and ISA |

## Reference deployments

Reference **platforms**, each with the library's share of the machine:

- **`laptop_unified`**: Apple-silicon laptop, unified memory, GPU shared with the caller's UI. Budget: 1 GB of buffers.
- **`server_dgpu`**: x86 with AVX-512 and a discrete GPU (16 GB). Budget: the whole GPU.
- **`sbc`**: ARM single-board computer, NEON, no usable GPU. Budget: 256 MB.

## Bounded consumers

None of its own: callers own display and storage. The library reports dropped or late frames
to the caller rather than queueing without bound.

## Recording guidance

Component files below this one follow the standard format, and also:

- Record bytes moved across `host_device_link` per operation: on discrete-GPU platforms
  transfers, not arithmetic, set the frame budget.

## Conventions

- Design decisions: PR description.
- ADRs: `docs/adr/`.
- Implementation choices: `docs/implementation-choices/`.

## Validation

Benchmarked per release on each reference platform at the supported values and at the
ceiling; caller survey yearly.
