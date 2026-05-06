import "./env.js";
import { createAuth } from "./auth.js";

type SeedUser = {
  email: string;
  password: string;
  name: string;
};

function parseSeedUsers(): SeedUser[] {
  const raw = process.env.BETTER_AUTH_SEED_USERS || "";
  return raw
    .split(";")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [email, password, name] = entry.split(":");
      if (!email || !password) {
        throw new Error(
          "Invalid BETTER_AUTH_SEED_USERS entry. Use email:password:name",
        );
      }
      return {
        email: email.trim().toLowerCase(),
        password,
        name: name?.trim() || email.trim().split("@")[0],
      };
    });
}

const seedUsers = parseSeedUsers();
if (seedUsers.length === 0) {
  console.log("No BETTER_AUTH_SEED_USERS configured; skipping auth seed.");
  process.exit(0);
}

const auth = createAuth({ disableSignUp: false });

for (const user of seedUsers) {
  try {
    await auth.api.signUpEmail({
      body: {
        email: user.email,
        password: user.password,
        name: user.name,
      },
    });
    console.log(`Seeded auth user: ${user.email}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message.toLowerCase().includes("already")) {
      console.log(`Auth user already exists: ${user.email}`);
      continue;
    }
    console.error(`Failed to seed auth user ${user.email}: ${message}`);
    process.exitCode = 1;
  }
}
