import "./env.js";
import express from "express";
import { fromNodeHeaders, toNodeHandler } from "better-auth/node";
import { auth } from "./auth.js";

const app = express();
const port = Number(process.env.AUTH_PORT || 3000);

app.all("/api/auth/*", toNodeHandler(auth));

app.get("/api/auth-session", async (req, res) => {
  const session = await auth.api.getSession({
    headers: fromNodeHeaders(req.headers),
  });

  if (!session) {
    return res.status(401).json({ authenticated: false });
  }

  return res.json({ authenticated: true, ...session });
});

app.get("/api/auth-health", (_req, res) => {
  res.json({ status: "ok" });
});

app.listen(port, "127.0.0.1", () => {
  console.log(`QuantLive auth service listening on 127.0.0.1:${port}`);
});
