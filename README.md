# 点名助手（源码）

给学委用的 Android 上课点名 / 考勤小工具：导入名单 → 每节课点状态（出勤、旷课、病假、事假、空课）→ 复制汇总或导出月度 Excel。数据只存在手机本地，不上传。

- 用 [Flet](https://flet.dev)（Python）编写，`flet==0.85.3`
- 安装包下载：<https://github.com/haoxiang1511-star/hmudq-attendance-release/releases/latest>
- 更新日志：[CHANGELOG.md](CHANGELOG.md)；发版与 version.json 说明：[tools/UPDATE_README.md](tools/UPDATE_README.md)

## 本地运行

```bash
python -m venv venv && source venv/bin/activate
pip install "flet==0.85.3" openpyxl pypinyin
flet run            # 桌面窗口预览
```

## 运行测试

```bash
python -m unittest discover -s tests
```

## 构建 APK（概要）

1. 装好 Flutter / Android SDK（见 Flet 官方文档 `flet build apk`）。
2. 打包前检查排除项：`python tools/check_apk.py`
3. `flet build apk`（应用名、build-number 等在 `pyproject.toml` 的 `[tool.flet]` 中）。
4. 签名请用**你自己的** keystore（例如 `--android-signing-key-store` 等参数）；本仓库不包含任何签名密钥。
   注意：用不同签名打出的包不能覆盖安装官方发布的安装包。

## 反馈

haoxiang1511@gmail.com
