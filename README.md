# lkfetch

Unofficial / 非官方 LibKey 辅助工具（开发中）。当前提交只有项目骨架，尚不能下载。后续再加入 Chrome session、LibKey resolver 和 PDF 下载。

设计边界：cookie 仅在进程内用于用户自己的浏览器会话；不提供 cookie dump，不写 cookie 文件，也不把凭据返回给 agent。当前版本不会读取 cookie。

需要 Python >=3.11，无第三方运行时依赖。在仓库根目录运行：

```sh
PYTHONPATH=src python3 -m lkfetch --help
```
