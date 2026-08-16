# 轻量个人知识库 品牌与第三方标识清理文档

> 本文档记录从「Yuunagi Library」向「轻量个人知识库」迁移时需要清理的品牌与第三方作者标识。
>
> 关联文档：[ui-refactor.md](ui-refactor.md)、[architecture.md](architecture.md)。

## 1. 目标

1. 将项目对外品牌从 **Yuunagi Library** 替换为 **轻量个人知识库**。
2. 清理仓库中不属于项目自身的第三方作者标识（`uynajgi`、`KKKKhazix`）。
3. 重新生成索引产物，确保 `data/pageindex/*.json` 中不再残留旧品牌文本。

## 2. 品牌替换清单（轻量个人知识库）

### 2.1 yuulibrary-desktop（桌面应用）

| 文件 | 替换内容 | 新值 |
|------|----------|------|
| `frontend/index.html` | `<title>` | `轻量个人知识库` |
| `app/main.py` | `webview.create_window` 标题 | `轻量个人知识库` |
| `app/http/server.py` | `FastAPI(title=...)` | `轻量个人知识库 Desktop` |
| `app/__init__.py` | 注释与 `__version__` 说明 | `轻量个人知识库` |
| `pyproject.toml` | `name` / `description` | `lqd-desktop` / `轻量个人知识库 Desktop — ...` |
| `yuulibrary-desktop.spec` | 注释、exe 名称说明 | `轻量个人知识库` |
| `run_app.py` | 注释 | `轻量个人知识库` |
| `README.md` | 标题与正文 | `轻量个人知识库` |
| `docs/architecture.md` | 标题与正文 | `轻量个人知识库` |
| `docs/development.md` | 标题与正文 | `轻量个人知识库` |
| `docs/deployment.md` | 标题与正文 | `轻量个人知识库` |
| `data/content/_index.md` | `title` frontmatter | `轻量个人知识库` |
| `data/content/about.md` | 标题与正文 | `轻量个人知识库` |
| `data/content/notes/welcome.md` | 标题与正文 | `轻量个人知识库` |
| `frontend/chat/*.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/chat/chat.css` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/library/library.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/manage/manage.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/upload/upload.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/config/config.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |
| `frontend/shared/render.js` | 注释中的 `Yuunagi Library` | `轻量个人知识库` |

### 2.2 yuulibrary-main（Hugo 静态站）

| 文件 | 替换内容 | 新值 |
|------|----------|------|
| `hugo.toml` | `title`、`copyright`、`baseURL`、`BookRepo` | `轻量个人知识库` |
| `README.md` | 标题、部署链接、badge | `轻量个人知识库` |
| `CLAUDE.md` | 标题 | `轻量个人知识库` |
| `CONTRIBUTING.md` | 标题 | `轻量个人知识库` |
| `content/_index.md` | `title` frontmatter | `轻量个人知识库` |
| `content/about.md` | 标题与正文 | `轻量个人知识库` |
| `content/notes/welcome.md` | 标题与正文 | `轻量个人知识库` |
| `assets/_custom.scss` | 注释 | `轻量个人知识库` |
| `scripts/migrate_mkdocs_to_hugo.py` | 默认标题 | `轻量个人知识库` |
| `layouts/partials/docs/footer.html` | `&copy;` 文案 | `轻量个人知识库` |
| `layouts/partials/docs/inject/menu-after.html` | 链接文案 | `轻量个人知识库` |

## 3. 第三方作者标识清理清单

需要全局删除或替换以下字符串：

- `uynajgi`
- `KKKKhazix`
- `github.com/uynajgi/yuulibrary`
- `github.com/KKKKhazix/khazix-skills`

### 3.1 yuulibrary-main

| 文件 | 清理内容 |
|------|----------|
| `hugo.toml` | `baseURL = 'https://uynajgi.github.io/yuulibrary/'`、`BookRepo = 'https://github.com/uynajgi/yuulibrary'` |
| `README.md` | 所有含 `uynajgi` 的 badge、链接 |
| `content/about.md` | `github.com/uynajgi/yuulibrary` 与 `github.com/KKKKhazix/khazix-skills` 链接 |
| `layouts/partials/docs/inject/menu-after.html` | GitHub 仓库链接（如含 `uynajgi`） |

### 3.2 yuulibrary-desktop

| 文件 | 清理内容 |
|------|----------|
| `data/content/about.md` | `github.com/uynajgi/yuulibrary` 与 `github.com/KKKKhazix/khazix-skills` 链接 |
| `data/content/notes/welcome.md` | 旧品牌与第三方链接 |

### 3.3 说明

- 数据文件中的书籍/论文作者署名（如 `Eric Jorgenson 著 · 赵灿 译`）属于内容本身，**不清理**。
- `app/vendor/` 中从 yuulibrary-main 复制的脚本如果包含第三方作者署名，需要检查并清理。

## 4. 内部代码前缀策略

为控制重构风险，**本次不强制全量替换 `yuu/Yuu` 内部前缀**：

- 新框架代码统一使用 `lqd/Lqd` 前缀；
- 旧业务代码在拆分和改造过程中，仅迁移与新功能直接相关的全局对象和 CSS 类；
- URL 路径（`/api/*`、`/pageindex/*`、`/raw/*`）保持不变；
- `localStorage` / `sessionStorage` key 从 `yuu_*` 迁移到 `lqd_*`，启动时做一次性迁移。

如果后续需要彻底去品牌化，可作为独立迭代进行全局替换。

## 5. 索引产物重建

`data/pageindex/*.json` 是生成的索引文件，其中可能包含「欢迎来到 Yuunagi Library」等旧品牌文本。修改 `data/content/` 后必须重新构建索引：

```bash
cd e:/知识库/yuulibrary-desktop
python -m app.main
# 在 Manage 视图点击「全量构建」
```

或命令行：

```bash
python -c "
from app.index.builder import build_full
from app.config.store import load_app_config
from dataclasses import replace
import os
data = os.path.abspath('data')
cfg = load_app_config(os.path.join(data, 'config'))
cfg = replace(cfg, content_dir=os.path.join(data, 'content'), pageindex_dir=os.path.join(data, 'pageindex'))
r = build_full(cfg.content_dir, cfg.pageindex_dir)
print(r.ok, r.docs_built)
"
```

## 6. 验证命令

清理完成后，使用以下命令验证无残留：

```bash
# 在 yuulibrary-desktop 目录
grep -R "Yuunagi Library" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" --include="*.spec" .
grep -R "uynajgi" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" --include="*.yml" --include="*.yaml" .
grep -R "KKKKhazix" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" --include="*.yml" --include="*.yaml" .

# 在 yuulibrary-main 目录
grep -R "Yuunagi Library" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" .
grep -R "uynajgi" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" .
grep -R "KKKKhazix" --include="*.py" --include="*.js" --include="*.css" --include="*.md" --include="*.html" --include="*.toml" .
```

## 7. 已知例外

以下情况保留旧品牌或第三方标识：

1. `yuulibrary-main/.gitmodules` 中的子模块来源（`alex-shpak/hugo-book`、`VectifyAI/PageIndex`）——这是第三方依赖，不是作者标识，保留。
2. 内容文件中的书籍/论文作者署名——属于内容本身，保留。
3. `dist/` / `build/` 构建产物——重新构建后会自动更新，不手工编辑。
4. `.git` 历史（如果未来初始化 git）——不在本次文件清理范围内。
