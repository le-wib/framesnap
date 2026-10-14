# Other interpolation models

What FrameSnap uses today, and what else exists. "Tested" means measured by the author on real
frames from an AI clip (13 frames hidden then rebuilt, PSNR against the original); everything
else is taken from the cited sources and has **not** been tried in FrameSnap.

## Used today

| Engine | Where it runs | Measured quality | Notes |
| --- | --- | --- | --- |
| **RIFE v4.6** through [rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan) (MIT) | GPU: Metal on macOS (the macOS build embeds MoltenVK), Vulkan driver on Windows (NVIDIA, AMD, Intel) | 35.4 dB, ahead by 2 to 4 dB on fast motion | last release of the tool: 2022-10-29 |
| **OpenCV DIS optical flow** | CPU | 34.7 dB, ahead on slow motion | automatic fallback, no GPU needed |
| ffmpeg `minterpolate` | CPU | 33.4 dB, about 2 s per frame | not used, kept as a reference |
| OpenCV Farneback | CPU | 33.1 dB | not used |
| Copy of the previous frame (what a held frame is) | | 26.6 dB | baseline |

## Candidates

| Model or tool | What it is | Why it could matter | What stands in the way |
| --- | --- | --- | --- |
| **Newer RIFE versions** (4.7 to 4.26, plus 4.25 heavy and 4.26 large) | same family and same ncnn runtime | the most direct upgrade: same Metal and Vulkan path, no new dependency. [TNTwise/rife-ncnn-vulkan](https://github.com/TNTwise/rife-ncnn-vulkan) and [styler00dollar/VapourSynth-RIFE-ncnn-Vulkan](https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan) list ncnn models up to 4.26, and [OpenFlowFrames](https://github.com/ZeroHackz/OpenFlowFrames) ships 4.9 to 4.26 on top of rife-ncnn-vulkan | needs a benchmark on real clips first, and a newer binary than the official one |
| **Apple VTFrameProcessor** (frame rate conversion, optical flow) | machine learning in Apple's VideoToolbox, macOS 15.4 and later, optimised for Apple Silicon ([WWDC25 session 300](https://nonstrict.eu/wwdcindex/wwdc2025/300/)); takes an interpolation phase, up to 8192 x 4320 | native, no model to ship | macOS only; needs a small Swift helper (not checked: whether PyObjC exposes it) |
| **GIMM-VFI** | flow based, arbitrary timestep, aimed at large motion | listed in [Real Video Enhancer](https://github.com/TNTwise/Real-Video-Enhancer) (ncnn, TensorRT) | heavier than RIFE; no turnkey cross-platform binary found |
| **FILM** (Google) | feature pyramid, good on large motion | strong reference for big displacements | ran out of memory at 4K in one benchmark ([SGM-VFI paper](https://arxiv.org/pdf/2404.06913)), slow |
| **EMA-VFI, AMT, IFRNet, SGM-VFI, BiFormer** | research models, PyTorch | higher scores than RIFE on some benchmarks (EMA-VFI base 30.85 dB against 29.87 dB for RIFE on X-Test-L-2K, but 132 ms against 10 ms per 512 x 512 frame on a 2080 Ti) | PyTorch only, very large downloads, no ncnn or Core ML builds found |
| **Diffusion based** (LDMVFI, EDEN and others) | generative interpolation | plausible results on very large motion | slow, research code |
| **TensorRT builds of RIFE** | NVIDIA only | the SVP project reports up to 100 percent faster than ncnn, after a long optimisation pass for every new resolution ([SVP wiki](https://svp-team.com/wiki/RIFE_AI_interpolation)) | NVIDIA only, heavy install |
| DaVinci Resolve Speed Warp, Topaz Chronos, Twixtor, After Effects Pixel Motion | commercial | very good results | cannot be bundled |

## Suggested next step

Benchmark RIFE 4.17, 4.25 and 4.26 against 4.6 on the same held frames (same method as above),
and only then decide whether to ship a newer binary and a model selector.
