import express from "express";
import cors from "cors";
import multer from "multer";
import dotenv from "dotenv";
import { mkdirSync, existsSync, readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
dotenv.config({ path: join(__dirname, ".env") });

const app = express();
const PORT = process.env.PORT || 3002;
const PRATIBHA_AGENT_URL = process.env.PRATIBHA_AGENT_URL || "http://localhost:8001";
const UPLOADS_DIR = join(__dirname, "..", "uploads");
const SUMMARIES_DIR = join(__dirname, "..", "summaries");

// Translate host Windows path → container path for the Python agent
function toContainerPath(hostPath) {
  const rel = hostPath.replace(UPLOADS_DIR, "").replace(/\\/g, "/");
  return `/app/uploads${rel}`;
}

app.use(cors());
app.use(express.json());
app.use(express.static(join(__dirname, "..")));

// ── CSV upload storage ─────────────────────────────────────────────────────
const pratibhaStorage = multer.diskStorage({
  destination: (req, file, cb) => {
    const date = new Date().toISOString().split("T")[0];
    const dir = join(UPLOADS_DIR, date);
    mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => cb(null, file.originalname),
});
const upload = multer({ storage: pratibhaStorage });

// ── Routes ─────────────────────────────────────────────────────────────────

// Upload 3 CSVs → parse → load into Postgres → return question count
app.post(
  "/api/pratibha/upload-export",
  upload.fields([
    { name: "activities_file", maxCount: 1 },
    { name: "sourcewise_file", maxCount: 1 },
    { name: "active_file", maxCount: 1 },
  ]),
  async (req, res) => {
    try {
      const files = req.files;
      if (!files.activities_file || !files.sourcewise_file || !files.active_file) {
        return res.status(400).json({ error: "All 3 files required" });
      }

      const response = await fetch(`${PRATIBHA_AGENT_URL}/parse-exports`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          activities_path: toContainerPath(files.activities_file[0].path),
          sourcewise_path: toContainerPath(files.sourcewise_file[0].path),
          active_path:     toContainerPath(files.active_file[0].path),
        }),
      });

      if (!response.ok) {
        const err = await response.text();
        return res.status(502).json({ error: err });
      }

      const data = await response.json();
      res.json(data);
    } catch (e) {
      console.error("upload-export error:", e);
      res.status(500).json({ error: e.message });
    }
  }
);

// Proxy chat to Python agent
app.post("/api/pratibha/chat", async (req, res) => {
  try {
    const response = await fetch(`${PRATIBHA_AGENT_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req.body),
    });
    const data = await response.json();
    res.json(data);
  } catch (e) {
    console.error("chat proxy error:", e);
    res.status(500).json({ error: e.message });
  }
});

// Trigger manual summary save
app.post("/api/pratibha/save-summary", async (req, res) => {
  try {
    const { date } = req.body;
    const response = await fetch(`${PRATIBHA_AGENT_URL}/save-summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date }),
    });
    const data = await response.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Fetch saved summary .md file
app.get("/api/pratibha/summary/:date", (req, res) => {
  const filePath = join(SUMMARIES_DIR, `summary_${req.params.date}.md`);
  if (!existsSync(filePath)) {
    return res.status(404).json({ error: "Summary not found for this date" });
  }
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.send(readFileSync(filePath, "utf8"));
});

// Health check
app.get("/api/health", (req, res) => res.json({ status: "ok" }));

app.listen(PORT, () => {
  console.log(`Pratibha backend running on port ${PORT}`);
});
