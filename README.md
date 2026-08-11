# Niwo - RenderEverything

把你和 AI 聊过的话题，整理成一个可以直接渲染成短视频的素材包。

这个 Skill 让 AI 帮你：定稿口播文案、联网下载真实图片和 B-roll 视频、用 ffmpeg 裁好镜头、写好素材说明，最后打包成一个 zip。你把 zip 上传到 Niwo，就能拿到成片。

<!-- TODO: 替换成正式的上传入口地址与产品名 -->

## 安装

```bash
npx skills add MontageAI/niwo-render-everything
```

支持 Claude Code、Codex、Cursor、OpenCode 等 70 多个 Agent。装到全局加 `-g`：

```bash
npx skills add MontageAI/niwo-render-everything -g
```

## 环境要求

这个 Skill 需要 Agent 具备三项能力，缺一项就跑不完：

1. 能联网访问网页并**下载图片、视频文件到本地**，不是只给链接
2. 能执行 shell 命令，环境里有 `ffmpeg` 和 `ffprobe`
3. 能把本地目录打包成 zip 交付给你

所以它适合 Claude Code、Codex、Cursor 这类有 shell 的编码 Agent。纯对话式的手机端 AI 助手通常做不到第一项。

macOS 装 ffmpeg：

```bash
brew install ffmpeg
```

## 用法

装好之后直接说你想做什么，比如：

> 帮我把 DeepSeek 涨价这件事做成一条一分钟的竖屏短视频

Agent 会先问你成片方向（竖屏、横屏还是方形）、目标时长、讲给谁看，然后给你一版文案草稿。文案定稿后它才开始找素材，最后交给你一个 zip。

## 产出物

```
content.json     # 文案、画幅、钩子标题、多音字注音
manifest.json    # 每个素材的画面说明、标签、来源、选段
images/
videos/
```

`content.json` 只包含**内容**。音色、语速、字幕、背景音乐这些渲染参数，由你在 Niwo 里渲染前自己选。

## 自校验

打包前 Agent 会跑一遍 `scripts/validate_bundle.py`，它检查字段合法性、manifest 与实际文件的对应关系、视频选段是否落在真实时长内，还能识破下载时拿到 HTML 错误页的情况。你也可以手动跑：

```bash
python3 skills/niwo-render-everything/scripts/validate_bundle.py <素材包目录>
```

## 关于这个仓库

这是自动同步的镜像仓库，内容来自 Niwo 的主仓库，**不接受 Pull Request**（提交会在下次同步时被覆盖）。有问题或建议请提 [Issue](https://github.com/MontageAI/niwo-render-everything/issues)。

## License

MIT
