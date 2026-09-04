// 前端构建：把静态文件拷进 dist 并编译 TypeScript。
// Tauri 的 frontendDist 指到 web/dist，node_modules 就不会被打进安装包。
import { cpSync, mkdirSync } from "node:fs";
import { execSync } from "node:child_process";

mkdirSync("dist", { recursive: true });
cpSync("index.html", "dist/index.html");
cpSync("style.css", "dist/style.css");
execSync("npx tsc -p tsconfig.json", { stdio: "inherit" });
console.log("[web] built dist/");