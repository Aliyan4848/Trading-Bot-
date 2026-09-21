import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard only talks to the backend API (NEXT_PUBLIC_API_URL).
  // Secrets never live in this build.
};

export default nextConfig;
