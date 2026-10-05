import { REGISTRY_VERSION, RUNTIME_VERSION, type PortalRegistry } from "../types";

function compatible(minimum: string): boolean {
  const parse = (value: string) => value.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const current = parse(RUNTIME_VERSION);
  const required = parse(minimum);
  for (let i = 0; i < Math.max(current.length, required.length); i += 1) {
    if ((current[i] || 0) > (required[i] || 0)) return true;
    if ((current[i] || 0) < (required[i] || 0)) return false;
  }
  return true;
}

export function validateRegistry(value: unknown): PortalRegistry {
  if (!value || typeof value !== "object") throw new Error("Registry is not an object");
  const registry = value as PortalRegistry;
  if (registry.registryVersion !== REGISTRY_VERSION) {
    throw new Error(`Unsupported registry version: ${registry.registryVersion || "unknown"}`);
  }
  if (!compatible(registry.minimumRuntimeVersion)) {
    throw new Error(`Registry requires Portal ${registry.minimumRuntimeVersion} or newer`);
  }
  if (!Array.isArray(registry.modules)) throw new Error("Registry modules must be an array");
  return registry;
}

export async function loadRegistry(): Promise<PortalRegistry> {
  const response = await fetch(`${import.meta.env.BASE_URL}config/registry.json`, {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok) throw new Error(`Unable to load portal registry (${response.status})`);
  return validateRegistry(await response.json());
}
