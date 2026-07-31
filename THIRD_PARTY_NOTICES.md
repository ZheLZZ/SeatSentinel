# Third-Party Notices

SeatSentinel 使用下列第三方软件或模型。本文件用于说明直接依赖及其许可来源，
不替代各项目随附的完整许可证文本。

| Component | Purpose | License |
| --- | --- | --- |
| [OpenVINO](https://github.com/openvinotoolkit/openvino) | 本地模型推理及 NPU/CPU 设备支持 | Apache-2.0 |
| [OpenVINO Telemetry](https://pypi.org/project/openvino-telemetry/) | OpenVINO 的可选工具依赖；SeatSentinel 不初始化其遥测 | Apache-2.0 |
| [Open Model Zoo](https://github.com/openvinotoolkit/open_model_zoo) `face-detection-retail-0004` | 人脸存在检测模型 | Apache-2.0 |
| [Open Model Zoo](https://github.com/openvinotoolkit/open_model_zoo) `landmarks-regression-retail-0009` | 本地人脸关键点定位 | Apache-2.0 |
| [Open Model Zoo](https://github.com/openvinotoolkit/open_model_zoo) `face-reidentification-retail-0095` | 本地 256 维人脸特征提取 | Apache-2.0 |
| [OpenCV](https://github.com/opencv/opencv) / `opencv-python` | 摄像头读取与图像处理 | Apache-2.0 |
| [NumPy](https://github.com/numpy/numpy) | 数组与张量处理 | BSD-3-Clause 及其发行包所列的附加许可 |
| [Pillow](https://github.com/python-pillow/Pillow) | Tkinter 调试画面和图标处理 | MIT-CMU |
| [pystray](https://github.com/moses-palmer/pystray) | Windows 托盘图标 | LGPL-3.0 |
| [cv2-enumerate-cameras](https://github.com/letmaik/cv2-enumerate-cameras) | Windows 摄像头枚举 | MIT |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 本地 EXE 构建工具 | GPL-2.0-or-later，附带应用分发特别例外 |
| [Python](https://www.python.org/) | 应用运行时 | Python Software Foundation License |

安装依赖时，Python 包管理器会取得各组件的许可证元数据。使用
`构建EXE.ps1` 生成并分发可执行文件的人员，应重新核对实际打包版本，保留
相应许可证及 NOTICE/COPYING 文件，并遵守所有传递性依赖的许可要求。

SeatSentinel 自有源代码采用项目根目录 `LICENSE` 中的 MIT License。第三方软件
和模型不因此改为 MIT，仍分别适用上表及其发行包所列许可证。
