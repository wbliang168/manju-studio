# 第三方组件与许可说明

本仓库只包含本项目自写的桌面 GUI 与编排代码。下面这些**不包含在仓库里**，由使用者自行安装，各自遵守原作者的许可。

| 组件 | 作用 | 许可 | 备注 |
| --- | --- | --- | --- |
| ComfyUI | 出图 / 出视频后端 | GPL-3.0 | 本项目只通过 `127.0.0.1:8188` 的 HTTP API 调用它，未复制其源码 |
| MiniMax H3 系列模型权重 | 视频生成 | 以模型发布页为准 | 几十 GB，仓库不含；商用或再分发前请自行确认授权 |
| ComfyUI 自定义节点（H3 / KJNodes 等） | 工作流节点 | 各自仓库为准 | 运行目录下的 `custom_nodes/` 不提交 |
| ComfyUI-Local-Infinite-Canvas | 无限画布前端 + 本地网关 | 见上游仓库 | 可选组件，默认只监听 127.0.0.1 |
| basketikun/infinite-canvas | 画布内核 | MIT | 上游前端已在 NOTICE 中注明 |
| comfystreamerh3 | FastH3 节点 | MIT | 可选，仅画布「H3 极速草稿」工作流使用 |
| edge-tts | 旁白 / 音色样本合成 | 见其仓库 | 运行时依赖 |
| AI Hive（`manju_skills/cloud.py`，可选） | 云端生成客户端 | 以服务方条款为准 | 默认不启用、不联网；API Key 只存本机 `config.json`，仓库不含 |
| Pillow / numpy / OpenCV / requests / imageio-ffmpeg | 通用 Python 库 | 各自许可 | 见 `requirements.txt` |
| Microsoft WebView2 Runtime / WebView2Loader | 内嵌网页 | 微软许可 | 二进制不随仓库分发 |

另外：

- `assets/haarcascade_frontalface_default.xml` 来自 OpenCV 的人脸检测数据，`assets/face_detection_yunet_2023mar.onnx` 来自 OpenCV Zoo，按各自上游许可使用。
- `icon.ico` 是本项目图标。
- `dist/漫剧工作室.exe` 是构建产物，不在仓库里；如需发布请自行打包，并在 Release 里附带许可说明。

如果打算把本项目用于商业用途，请先逐项核对上述上游授权，尤其是模型权重。
