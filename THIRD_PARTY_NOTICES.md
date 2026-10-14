# Third party components

FrameSnap is released under GPL-3.0-or-later. The apps bundle the following components.

| Component | Use | License |
| --- | --- | --- |
| [FFmpeg](https://ffmpeg.org) static build with x264, via the `imageio-ffmpeg` package | decoding, encoding, audio | GPL (the reason FrameSnap is GPL) |
| [rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan) 20221029 (executable and the `rife-v4.6` model) | GPU frame interpolation | MIT, copyright (c) 2020 nihui. The full text ships next to the executable (`rife/LICENSE`) |
| [RIFE](https://github.com/hzwer/ECCV2022-RIFE) (model weights of v4.6) | the neural network run by rife-ncnn-vulkan | MIT |
| [ncnn](https://github.com/Tencent/ncnn) and [MoltenVK](https://github.com/KhronosGroup/MoltenVK) (inside the rife-ncnn-vulkan executables) | neural network runtime, Vulkan on top of Metal on macOS | BSD-3-Clause and Apache-2.0 |
| [OpenCV](https://opencv.org) (`opencv-python-headless`) | CPU optical flow (DIS) and image handling | Apache-2.0 |
| [NumPy](https://numpy.org) | arrays | BSD-3-Clause |
| [pywebview](https://pywebview.flowrl.com) | the window | BSD-3-Clause |
| [PyInstaller](https://pyinstaller.org) | packaging | GPL-2.0-or-later with a bootloader exception |

The rife-ncnn-vulkan release is downloaded at build time by `packaging/fetch_rife.py`
and verified against a pinned SHA-256 checksum.
