export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

export async function apiFetch<T>(path: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;
  const res = await fetch(`${API_URL}${path}`, { headers, cache: "no-store" });
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${await res.text().catch(() => "")}`);
  }
  return res.json() as Promise<T>;
}
