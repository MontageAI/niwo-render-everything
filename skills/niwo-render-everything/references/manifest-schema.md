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
- `assets`：素材条目数组，最多 200 条。

### assets 条目

- `file`：**必填**，相对素材包根目录的路径，例如 `images/01-office.jpg`。必须是相对路径，不能是绝对路径、URL 或包含 `..`。同一个文件不能声明两次。
- `kind`：**必填**，`image` 或 `video`，必须与扩展名一致。图片支持 `.jpg` `.jpeg` `.png` `.webp` `.gif`，视频支持 `.mp4` `.mov` `.mkv` `.webm`。
- `summary`：**单行**中文，一句话说清画面里看得见的东西（主体、地点、动作、画面类型），不要写你的解读。Niwo 会直接拿它当素材说明用，写得准才配得准。上限 240 字。
- `tags`：2 到 5 个中文短标签，每个不超过 32 字，最多 8 个。
- `source_url`：素材所在那一页的地址（不是网站首页），方便回溯来源，能填就填。
- `script_anchor`：**从口播文案里原样摘一小句**，表示这个素材适合配在这句话附近，可以留空。注意这只是建议：最终分几个镜头、素材落在第几镜由 Niwo 决定，不要试图在这里排完整的分镜表。上限 240 字。
- `clip_start_seconds` / `clip_end_seconds`：视频选段窗口，单位秒。终点必须大于起点。**图片不能写这两个字段**，写了直接校验失败。
- `license`：可选的授权声明，仅用于留档，上限 64 字。
- `relevance_score`：可选，0 到 1 之间。一般不用填。

## 容易踩的坑

- `summary` 写成多行会被压成单行，所以别在里面排版。
- 已经按第三步裁好的视频**不要**写 `clip_start_seconds` / `clip_end_seconds`。只有交的是一段较长的原片、希望 Niwo 从中取某一段时才写。
- 交长原片却写了 `summary` 和 `tags`、又没写选段时间，Niwo 会认为你已经挑好了镜头，直接从第 0 秒开始取，大概率取到片头。
- `manifest.json` 里声明的文件必须真实存在。反过来，磁盘上多出来的文件不写进 manifest 也能用，Niwo 会自动补描述。
