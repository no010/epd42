// 前端构建：把静态文件拷进 dist 并编译 TypeScript。
// Tauri 的 frontendDist 指到 web/dist，node_modules 就不会被打进安装包。
// 注意：tauri build 会以 src-tauri 为 cwd 调用本脚本，因此所有路径都必须基于
// 本脚本所在目录（import.meta.url）解析，不能用相对进程 cwd 的路径。
import { cpSync, mkdirSync } from "node:fs";
import { execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// scripts/build.mjs 的父目录即 web/
const dir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const dist = path.join(dir, "dist");

mkdirSync(dist, { recursive: true });
cpSync(path.join(dir, "index.html"), path.join(dist, "index.html"));
cpSync(path.join(dir, "style.css"), path.join(dist, "style.css"));
execSync("npx tsc -p tsconfig.json", { stdio: "inherit", cwd: dir });
console.log("[web] built dist/");