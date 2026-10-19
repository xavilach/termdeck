// Lints the page's scripts as one program, in the order of index.html (they share their names), and
// the pure helpers on their own. Errors name the file and the line in it.
const fs = require("fs");
const path = require("path");
const { ESLint } = require("eslint");

const web = path.join(__dirname, "..", "..", "web");
const scripts = [...fs.readFileSync(path.join(web, "index.html"), "utf8").matchAll(/<script src="\/([^"]+)"/g)]
  .map((m) => m[1]).filter((f) => !f.startsWith("vendor/") && f !== "format.js");

// where each script starts in the joined text
const parts = scripts.map((file) => ({ file, text: fs.readFileSync(path.join(web, file), "utf8") }));
let line = 1;
for (const part of parts) { part.first = line; line += part.text.split("\n").length; }
const joined = parts.map((p) => p.text).join("\n");
const where = (n) => { const p = [...parts].reverse().find((x) => x.first <= n); return `${p.file}:${n - p.first + 1}`; };

(async () => {
  const eslint = new ESLint({ overrideConfigFile: path.join(__dirname, "eslint.config.js"), cwd: path.join(__dirname, "..", "..") });
  const results = [
    ...(await eslint.lintText(joined, { filePath: path.join(web, "page.js") })),
    ...(await eslint.lintFiles([path.join(web, "format.js")])),
  ];
  let problems = 0;
  for (const r of results) {
    for (const m of r.messages) {
      problems++;
      const at = r.filePath.endsWith("page.js") ? where(m.line) : `${path.basename(r.filePath)}:${m.line}`;
      console.log(`${at}  ${m.ruleId}: ${m.message}`);
    }
  }
  console.log(problems ? `${problems} problem(s)` : `lint ok: ${scripts.join(", ")}, format.js`);
  process.exit(problems ? 1 : 0);
})();
