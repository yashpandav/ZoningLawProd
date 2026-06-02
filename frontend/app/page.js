import ZoningMap from "./components/ZoningMap";

export default function Page() {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
  return <ZoningMap apiBase={apiBase} />;
}
