# OpenWolf

@.wolf/OPENWOLF.md

This project uses OpenWolf for context management. Read and follow .wolf/OPENWOLF.md every session. Check .wolf/cerebrum.md before generating code. Check .wolf/anatomy.md before reading files.

## 工作流默认（用户 2026-08-26 指定）
- 代码改动经用户确认方向后，**默认由 AI 直接 `git commit` 并 `git push` 到 `origin/main`**，无需每步确认。
- 仅在涉及不可逆/强外部副作用（如 `git push --force`、删除数据、对外发布）或用户明确要求时再确认。
