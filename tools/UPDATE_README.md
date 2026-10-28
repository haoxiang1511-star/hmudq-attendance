# 检查更新：version.json 说明

App 里「关于软件 → 检查更新」只在手动点击时联网，按 `main.py` 里 `UPDATE_CONFIG["version_urls"]` 的顺序
（先 Gitee raw，再 GitHub raw）读取一个很小的 `version.json`，任一地址成功即用，每个地址超时 10 秒（两边都超时最多约 20 秒才提示「检查失败」）。
App 只读这个 JSON、比较版本，然后用系统浏览器打开下载链接；**不会在 App 里下载或执行任何代码**。

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 语义版本号，如 `"1.4.1"`（可带 v） |
| `build` | 是 | 整数 build-number，与 pyproject `build_number`、`APP_BUILD` 一致，只增不减。App 先比 build，相同再比 version |
| `notes` | 否 | 更新说明：字符串，或字符串数组（每条显示为一行「• …」），最多显示 2000 字 |
| `gitee_apk_url` | 否 | **国内直链（优先）**：Gitee Release 附件下载地址。Gitee **没有**类似 GitHub 的 `latest` 永久链，每次发版写死该 tag，例如 `https://gitee.com/Hector1511/hmudq-attendance-release/releases/download/v1.5.1/hmudq-attendance.apk`。更新弹窗第一按钮 |
| `github_apk_url` | 否 | GitHub 直接下载链接，固定写成 `https://github.com/haoxiang1511-star/hmudq-attendance-release/releases/latest/download/hmudq-attendance.apk`：每次发版把 APK 以**同一个文件名**传到新 Release，这个链接就永远指向最新版。更新弹窗第二按钮 |
| `lanzou_url` | 否 | 蓝奏云文件夹分享链接。固定为 `https://wwbwu.lanzoup.com/b01i5ycuyb`（包里也内置了这个，永不更换）。更新弹窗第三按钮 |
| `lanzou_code` | 否 | 蓝奏云提取码，1–12 位字母数字，如 `"dt9b"`。不写或格式不对时用内置的 `default_lanzou_code`。更新弹窗里显示在「蓝奏云下载」按钮下方，带「复制」按钮 |

下载链接必须是 `https://`。version.json 里没写（或不是 https）时，用 `main.py` 里 `UPDATE_CONFIG` 的内置地址：
`default_gitee_apk_url`、`default_github_apk_url`、`default_lanzou_url`、`default_lanzou_code`。某个地址没有的那个按钮不显示；
三个都没有则视为这个版本信息无效。弹窗按钮顺序：**国内直链（Gitee）→ GitHub → 蓝奏云**。

## 放在哪里

同一份 `version.json` 放两处（内容保持一致），App 内置的读取顺序是先 Gitee 再 GitHub：

- Gitee：仓库 `Hector1511/hmudq-attendance-release`，**master** 分支根目录
  → `https://gitee.com/Hector1511/hmudq-attendance-release/raw/master/version.json`
- GitHub：仓库 `haoxiang1511-star/hmudq-attendance-release`，main 分支根目录
  → `https://raw.githubusercontent.com/haoxiang1511-star/hmudq-attendance-release/main/version.json`

这两个仓库只放 version.json 和安装包，不放源代码。

## 每次发版

> **两条硬规定**
> - **build 号每次发版必须 +1**（pyproject `build_number`、main.py `APP_BUILD`、version.json `build` 三处一致）。
>   App 先比 build、build 相同才比 version：只改 version 不加 build，旧版 App **检测不到**新版；build 变小还会被判为「已是最新」。
> - **蓝奏云文件命名固定为 `点名助手_vX.Y.Z.apk`**（例如 `点名助手_v1.4.1.apk`），并让它排在文件夹**最上面**。
>   更新弹窗里的说明写的就是「在文件夹里选最上面那个（版本号最大的）安装包下载」。

1. 打好 APK（build 只增不减，About 页版本与 CHANGELOG 一致），先用 `tools/check_apk.py` 检查包里没有数据库/名单。
2. **蓝奏云**：把新安装包按 `点名助手_vX.Y.Z.apk` 命名后上传到「点名助手」文件夹（分享链接 `https://wwbwu.lanzoup.com/b01i5ycuyb`，提取码 `dt9b`），
   让最新版排在文件夹最上面。文件夹链接永不更换，不需要改代码。
3. **Gitee**：在 `Hector1511/hmudq-attendance-release` 新建 Release（tag 如 `v1.5.1`），上传 APK，
   **附件文件名固定为 `hmudq-attendance.apk`**（单文件 ≤100MB）。把浏览器可点的公开直链写入 version.json 的 `gitee_apk_url`
   （以及代码里的 `default_gitee_apk_url`）。Gitee 无 `latest` 永久链，每次发版写死该 tag 的 download URL。
4. **GitHub**：在 `haoxiang1511-star/hmudq-attendance-release` 新建 Release（tag 如 `v1.5.1`），上传 APK，
   **资产文件名固定为 `hmudq-attendance.apk`**，这样 `releases/latest/download/hmudq-attendance.apk` 永远指向最新版。
5. **version.json**：改 `version` / `build` / `notes` / `gitee_apk_url`，同时推到 Gitee（master 分支）和 GitHub（main 分支），两边内容一致。
   `github_apk_url`、`lanzou_url`、`lanzou_code` 一般不用改；`gitee_apk_url` **每次发版都要改成新 tag**。
6. 用旧版 App 点「检查更新」实际验一遍：能弹出新版，三个下载按钮（有 URL 的）都能打开；国内优先点 Gitee。

样例见同目录 `version.json`。

## 附：电脑上生成恢复文件

`python tools/make_restore_file.py 源.db [输出.db]`：把电脑上（脚本生成/修改过的）attendance.db 打成 App
「从文件恢复」能识别的标准恢复文件，写入与 App 备份相同的标识，并用 App 同一个 `validate_backup` 校验；不合格不生成文件。
