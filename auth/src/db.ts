import { Pool, type PoolConfig } from "pg";

function normalizeDatabaseUrl(rawUrl: string): string {
  const url = rawUrl.replace("postgresql+asyncpg://", "postgresql://");
  const parsed = new URL(url);
  parsed.searchParams.delete("channel_binding");
  return parsed.toString();
}

export function getDatabaseUrl(): string {
  const databaseUrl = process.env.BETTER_AUTH_DATABASE_URL || process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("DATABASE_URL or BETTER_AUTH_DATABASE_URL is required");
  }
  return normalizeDatabaseUrl(databaseUrl);
}

export function createPool(): Pool {
  const connectionString = getDatabaseUrl();
  const sslRequired =
    connectionString.includes("sslmode=require") ||
    connectionString.includes("neon.tech");

  const config: PoolConfig = { connectionString };
  if (sslRequired) {
    config.ssl = { rejectUnauthorized: false };
  }

  return new Pool(config);
}
