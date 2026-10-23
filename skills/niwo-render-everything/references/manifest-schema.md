# manifest.json 字段协议

素材包根目录下的 `manifest.json` 逐条声明素材文件及其画面说明。这份 JSON **不接受任何未列出的字段**。

## 模板

```json
{
  "schema_version": 1,
  "generated_by": "你的名字，例如 claude-code",
  "notes": "可选，写给用户看的补充说明",
  "assets": [
    {
      "file": "images/01-office.jpg",
      "kind": "image",
      "summary": "写字楼玻璃幕墙外景，白天仰拍",
      "tags": ["写字楼", "城市", "外景"],
      "source_url": "https://example.com/page-you-took-it-from",
      "script_anchor": "所有人都开始主动为平台生产内容"
    },
    {
      "file": "videos/01-crowd.mp4",
      "kind": "video",
      "summary": "地铁站人群快速走动，画面前景虚化",
      "tags": ["人群", "地铁", "通勤"],
      "source_url": "https://example.com/page-you-took-it-from",
      "script_anchor": "它抢的是电视的时间"
    }
  ]
}
```

## 字段说明

### 顶层

- `schema_version`：固定 `1`。
- `generated_by`：生成这个素材包的工具名，仅用于留档，上限 200 字。
- `notes`：可选的补充说明，仅用于留档，上限 2000 字。
- `assets`：素材条目数组，协议最多 200 条；Niwo 上传媒体总数最多 100 个，实际交付遵守 100 个限制。

### assets 条目

- `file`：**必填**，相对素材包根目录的路径，例如 `images/01-office.jpg`。必须是相对路径，不能是绝对路径、URL 或包含 `..`。同一个文件不能声明两次。
- `kind`：**必填**，`image` 或 `video`，必须与扩展名一致。图片支持 `.jpg` `.jpeg` `.png` `.webp` `.gif`，视频支持 `.mp4` `.mov` `.mkv` `.webm`。
- `summary`：**单行**中文，一句话说清画面里看得见的东西（主体、地点、动作、画面类型），不要写你的解读。Niwo 会直接拿它当素材说明用，写得准才配得准。上限 240 字。
- `tags`：2 到 5 个中文短标签，每个不超过 32 字，最多 8 个。
- `source_url`：素材所在那一页的地址（不是网站首页），方便回溯来源，能填就填。最多 2000 字符；超限由外部 Agent 改用仍指向同一素材来源的有效短网址，不能直接截断网址。
- `script_anchor`：**从口播文案里原样摘一小句**，表示这个素材适合配在这句话附近，可以留空。注意这只是建议：最终分几个镜头、素材落在第几镜由 Niwo 决定，不要试图在这里排完整的分镜表。最多 512 字符，建议控制在 240 字符以内；超限由外部 Agent 重新选择较短的连续原文，不能改写定稿口播。7.13 拒收超过 512 字符的输入，通过后仍压成单行并截到 240 字符；Niwo 上传接口同步使用 512 字符输入上限。
- `clip_start_seconds` / `clip_end_seconds`：视频选段窗口，单位秒。终点必须大于起点。**图片不能写这两个字段**，写了直接校验失败。
- `license`：可选的授权声明，仅用于留档，最多 128 字符。超限由外部 Agent 精简说明、保留许可类型与必要署名，不得为通过校验而改成不真实的授权声明。
- `relevance_score`：可选，0 到 1 之间。一般不用填。

### editorial

仅当用户明确选择横屏信息动效版 `landscape-editorial-motion-v1`，在该版面的素材条目中填写。其他版面沿用原字段。示例视频底片与视频来源的透明前景：

```json
{
  "file": "videos/01-scene.mp4",
  "kind": "video",
  "editorial": {
    "role": "footage",
    "subject": "镜头中实际可见的主体",
    "segment_ids": ["opening"],
    "title_candidate": true,
    "focus": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8}
  }
}
```

```json
{
  "file": "images/01-subject.png",
  "kind": "image",
  "editorial": {
    "role": "foreground",
    "subject": "从镜头中抠出的主体",
    "segment_ids": ["opening"],
    "derived_from": {"file": "videos/01-scene.mp4", "at_seconds": 2.4}
  }
}
```

- `role`：必填，`footage` 是真实视频底片，`foreground` 是有可见主体和透明背景的 PNG 叠层，`reference_image` 是普通软件截图、图表或参考图片。后者必须为 image，不要求透明、不声明 derived_from，不计入视频容量，不替代必需的透明前景。
- `segment_ids`：关联 `content.editorial.segments` 中已有的 `id`；视频应关联其要支撑的口播段落。最多 128 项。
- `subject`：可选，真实可见的主体，最多 2000 字符。
- `title_candidate`：可选布尔值，提示素材是否适合片名画面，默认 `false`。
- `derived_from`：前景必填，`file` 必须原样指向本包 manifest 已声明的视频安全相对路径，`at_seconds` 是该文件中的非负抽帧秒数。路径最多 260 字符；抽帧位置必须落在文件实际时长内。
- `focus`：可选主体框；`x`、`y` 是源画面中的左上角坐标，`width`、`height` 是宽高，均用 0 到 1 的有限数值，必须有正面积且不越出画面。

`foreground` 必须从其 `derived_from` 视频实际抽帧抠图，不能用独立图片、截图或生成图冒充。视频有效时长按实际文件与选段窗口计算，透明前景不增加视频时长。完整步骤见 [专属备料说明](editorial-motion-preparation.md)。

`footage` 交付真实视频底片；Agent 不得先把新标题、卡片、软件截图或动效合进视频再标为底片。真实软件截图应作为独立 `image` 素材交付，并在信息动效包中声明 `editorial: {"role": "reference_image", "segment_ids": ["solution"]}`（段落 ID 必须真实存在）；`foreground` 也保持独立 PNG，由 Niwo 合成。`title_candidate` 仅是用途提示，不要求把片名提前渲染到素材中。真实来源本来带有的文字与界面应在 `summary` 中如实说明。

## 容易踩的坑

- `summary` 写成多行会被压成单行，所以别在里面排版。
- 已经按第三步裁好的视频**不要**写 `clip_start_seconds` / `clip_end_seconds`。只有交的是一段较长的原片、希望 Niwo 从中取某一段时才写。
- 交长原片却写了 `summary` 和 `tags`、又没写选段时间，Niwo 会认为你已经挑好了镜头，直接从第 0 秒开始取，大概率取到片头。
- `manifest.json` 里声明的文件必须真实存在。普通包的未声明媒体可回落补描述；信息动效包必须声明全部媒体及其用途，漏写应由外部 Agent 补齐后复检。
