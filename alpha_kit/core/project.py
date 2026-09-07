"""项目根：`repos/` 与 `storage/` 相对于谁（architecture.md §二 / §十二）。

引擎此前没有"项目根"这个概念, 一切相对路径都对着**当前工作目录**解析:
`find_region` 用 `Path.cwd()` 去 glob `repos/*/regions/`, `--store` 的缺省是字面量
`storage/l3`。于是在仓库根下一切正常, 换到任何子目录就全塌:

    cd storage && ak store status
    FileNotFoundError: 'storage/l3/us/_axes/sessions.json'      # 实为 storage/storage/l3/…

而且是**双重静默**: region 文件找不到就退回 `{}`（于是 l3_root / pnl_out / halt_proxy
这些口径统统失效, 悄悄改用 argparse 缺省）, 然后 store 路径也错, 最后以一个 json
读文件的裸 traceback 收场——三层里没有一层说得出"你不在项目根"。

研究员会在 `repos/g_yliu/nodes/...` 里待着写节点, 在那儿敲一条 `run` 是最自然不过的
事。工具必须能自己找到根, 而不是要求人先 `cd` 回去。
"""
from __future__ import annotations

import os
from pathlib import Path

# 根的判据：既有 repos/（region 与节点 config 住在它下面）, 又有一个仓库标记。
#
# 只认 `repos/` 是不够的: 那是开发机上极常见的顶层目录名, 而 macOS 的 APFS 默认
# 大小写不敏感——`~/Repos` 会让 `(d/"repos").is_dir()` 为真, 于是在 $HOME 下任何
# 地方敲 ak 都会把家目录当成项目根, 然后去找 $HOME/storage/l3/us。Linux 上同样的
# 布局却返回 None。两条判据一起要求, 这个假阳性就没了。
MARKER = "repos"
REPO_MARKERS = ("pyproject.toml", ".git")
ENV = "ALPHAKIT_ROOT"


def find_root(start: str | Path | None = None) -> Path | None:
    """从 `start`（缺省 cwd）逐级向上找项目根；找不到返回 None。

    `ALPHAKIT_ROOT` 优先——引擎被当作库装进别处、repos 不在仓库里时的逃生口。
    """
    env = os.environ.get(ENV)
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / MARKER).is_dir() and any((d / m).exists() for m in REPO_MARKERS):
            return d
    return None


def anchor(path: str | Path, root: Path | None = None) -> Path:
    """把相对路径钉到项目根上；绝对路径原样返回。

    找不到根时退回 cwd——保持旧行为, 不在这里抛错: 报错的时机应当是"真的要用它却
    没有"（Store 打不开轴时），那时报出来的信息比这里丰富得多。
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    base = root or find_root()
    return (base / p) if base else (Path.cwd() / p)
