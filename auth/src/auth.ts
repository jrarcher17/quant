import "./env.js";
import { betterAuth } from "better-auth";
import { createPool } from "./db.js";

const baseURL = process.env.BETTER_AUTH_URL || "http://127.0.0.1:8080";
const secret = process.env.BETTER_AUTH_SECRET;

if (!secret) {
  throw new Error("BETTER_AUTH_SECRET is required");
}

export function createAuth(options?: { disableSignUp?: boolean }) {
  return betterAuth({
    appName: "ApexQ",
    baseURL,
    basePath: "/api/auth",
    secret,
    database: createPool(),
    trustedOrigins: [
      baseURL,
      "http://127.0.0.1:8080",
      "http://localhost:8080",
    ],
    emailAndPassword: {
      enabled: true,
      disableSignUp: options?.disableSignUp ?? true,
      minPasswordLength: 8,
      maxPasswordLength: 128,
    },
    session: {
      expiresIn: 60 * 60 * 24 * 7,
      updateAge: 60 * 60 * 24,
    },
  });
}

export const auth = createAuth();
