# AACR 本地数据

项目自带的原始标注数据，用于独立运行评测。两份 JSON 逐字节复制自用户指定的 AACR 数据，无需原 AACR 或 Go 项目目录。

- `positive_samples.json`：196 个 PR 条目，1506 条评论，赋 label=1。
- `negative_samples.json`：155 个 PR 条目，639 条评论，赋 label=0。
- 合并：2145 条评论，50 个仓库，200 个 PR。

正/负文件可能包含相同 PR，不能把条目数量相加当作 PR 总数。源码根据 PR 的仓库身份另行 clone/fetch 到本项目 `.cache/benchmark-repositories`，按 source_commit→target_commit 评审。

文件 SHA256：

```text
positive_samples.json 7a4a0e7046ffd1b8f41f951480bbb618d23d38d9f67364f38aabcda121a50be3
negative_samples.json 3859efe063c0e59852113c64feb81c18b1a1fcd2ce9d92d08a48e9d8d9879ebd
```

评测以这两份文件的内容指纹冻结身份，参考评论只给 Judge，不传给评审器。
