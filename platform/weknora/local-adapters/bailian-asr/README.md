# 百炼 ASR 协议适配

范围：将 WeKnora v0.8.0 的 `POST /v1/audio/transcriptions` multipart 上传转换成百炼原生 `multimodal-generation/generation` 请求。支持 `qwen-audio-3.0-asr-flash`；不冒充异步 filetrans 或 OpenAI whisper。

## 部署

服务复用固定版本 `wechatopenai/weknora-app:v0.8.0` 中已有 Python/FFmpeg，以非 root 用户运行，源码只读挂载，不运行 WeKnora 主程序。无宿主机映射端口，不挂载数据库或密钥文件。

WeKnora 模型：ASR / remote / generic，Base URL `http://bailian-asr:8080/v1`，模型名称 `qwen-audio-3.0-asr-flash`，API Key 使用百炼相应地域凭据。系统出站白名单仅追加精确服务名 `bailian-asr`。凭据仍由 WeKnora 加密保存，每次请求通过内网传给适配器，适配器仅转发到固定百炼官方 HTTPS 地址，不落盘、不打印。

## 输入与输出边界

- 单文件 <=50 MiB、<=20 分钟、并发 2；上传及解码失败返回明确错误。
- FFmpeg 先转换为 16kHz 单声道 WAV，按 240 秒顺序切分，每段低于百炼同步接口 5 分钟限制；原文件不改变。
- 采用无重叠窗口，不做模型润色、补写或跨段去重。切点可能影响跨段词句，正式长录音质量须抽查。
- 总处理预算 250 秒，每个上游请求最多 90 秒。超时应分短文件重试；不能保证任意 20 分钟音频都在 WeKnora 的 300 秒调用窗口内完成。
- 任何分段失败则整次返回错误，不把部分结果标为成功；没有自动重试，避免重复收费。
- 返回 `text` 和 `segments`；segment 起止为音频窗口范围，**不是逐句或逐字对齐时间戳**。不提供说话人分离。
- 即使底层 FFmpeg 可读视频音轨，也只提取音频；不解析视频画面，且 WeKnora 是否允许上传 MP4 是独立限制。
- 不改变原有 OpenAI ASR 配置。模型能力和计费按百炼官方账户权限确定。

## 验证

`python3 -m unittest discover -s local-adapters/bailian-asr -v`

官方协议依据：https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide （Qwen-Audio-3.0-ASR-Flash 同步调用章节）。

## 本次实测（2026-09-21）

9项自动测试通过。通过 WeKnora 模型调试 API 调用百炼的人工合成中文语音（8.392688秒），约1810ms成功返回39字，数字12345识别正确。样本与响应在 `verification-artifacts/bailian-asr-20260921/`。该结果证明真实API协议链路，不是业务音频准确率或长音频时延验收。

原会议录音片段上传测试被自动审批拒绝，未执行；用户需明确授权该具体音频发送至百炼后再测。未修改知识库ASR选择、未触发已上传资料重解析。新文件上传时在解析配置中选择本模型。

## 回退

停用或移除新建ASR模型（ID见deployment.json），停止 `docker compose stop bailian-asr`。恢复配置时只删除本次新增服务段，保留用户后续修改；旧compose备份路径见deployment.json。出站白名单只移除本次新增的 `bailian-asr`，不要覆盖后续规则。原聊天/Embedding/视觉/重排模型均未修改。
