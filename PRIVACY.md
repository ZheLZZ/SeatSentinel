# SeatSentinel 隐私说明

适用版本：`v0.1.1-beta`

SeatSentinel 的设计目标是只在本机判断用户是否仍在电脑前，并在满足安全条件时
调用 Windows 锁屏。SeatSentinel 不提供云端服务，也不要求用户账户。

## 摄像头数据

- 摄像头帧只在当前进程内存中处理；
- OpenVINO 推理在本机 NPU 或 CPU 上执行；
- 调试窗口只读取监控线程维护的“最新帧”内存副本；
- 不保存照片，不录制视频，不将画面写入日志；
- 锁屏、暂停、摄像头释放和程序退出时立即清空最新帧；
- 关闭调试窗口不会创建或保留历史画面。

SeatSentinel 只做人脸存在检测，不进行身份识别，不建立人脸库，也不生成可用于
识别个人身份的人脸特征模板。

## 本地保存的数据

SeatSentinel 可能在本地保存：

- 用户设置：摄像头名称、推理设备、阈值、时间和分辨率；
- 运行日志：启动、停止、设备选择、故障和锁屏状态。

日志不包含摄像头画面。日志可能包含摄像头设备名称、OpenVINO 设备名称和
异常文本，用户在公开日志前应自行检查。

源码运行时，设置通常位于项目目录的 `settings.json`；该文件已被 Git 忽略。
打包版默认使用：

```text
%LOCALAPPDATA%\SeatSentinel\settings.json
%LOCALAPPDATA%\SeatSentinel\logs\seat-sentinel.log
```

首次运行打包版时，如果新目录还没有设置文件，程序会只读检查旧版应用目录，
并将找到的设置复制到 SeatSentinel 目录。旧目录及其日志不会被删除或上传。

## 网络边界

SeatSentinel 的正常监控代码不包含上传、远程推理或网络 API 调用。

`一键启动.ps1` 仅在以下情况下联网：

1. 从 PyPI 或配置的镜像安装 Python 依赖；
2. 从 Intel Open Model Zoo 官方存储地址下载人脸检测模型。

下载后的模型必须通过固定 SHA-256 校验。

OpenVINO 包含供其转换工具使用的可选遥测组件。SeatSentinel 不初始化这些转换
工具，并在导入 OpenVINO 前设置进程级 `DO_NOT_TRACK=1` 和
`SCARF_NO_ANALYTICS=1`。这些设置仅影响 SeatSentinel 进程，不修改用户的系统级
OpenVINO 偏好。

## 第三方组件

SeatSentinel 依赖 OpenVINO、OpenCV 等第三方开源组件。组件清单和许可信息见
`THIRD_PARTY_NOTICES.md`。第三方软件自身的行为和政策由其维护者负责。

## 隐私问题

如果发现 SeatSentinel 写入、保留或传输了本说明未披露的数据，请按照
`SECURITY.md` 中的方式报告，并避免在公开 Issue 中附加摄像头画面、日志中的
个人路径或其他敏感信息。
