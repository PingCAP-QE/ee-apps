export function getPointer(value: unknown, pointer = ""): unknown {
  if (pointer === "" || pointer === "/") return value;
  if (!pointer.startsWith("/")) throw new Error(`Invalid JSON Pointer: ${pointer}`);
  return pointer
    .slice(1)
    .split("/")
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"))
    .reduce<unknown>((current, key) => {
      if (current === null || current === undefined || typeof current !== "object") return undefined;
      return (current as Record<string, unknown>)[key];
    }, value);
}

export function setPointer(target: Record<string, unknown>, pointer: string, value: unknown): void {
  const keys = pointer.replace(/^\//, "").split("/").filter(Boolean);
  let current = target;
  keys.forEach((key, index) => {
    if (index === keys.length - 1) {
      current[key] = value;
    } else {
      const next = current[key];
      if (!next || typeof next !== "object") current[key] = {};
      current = current[key] as Record<string, unknown>;
    }
  });
}

export function interpolate(template: string, values: Record<string, unknown>): string {
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key: string) =>
    encodeURIComponent(String(values[key] ?? "")),
  );
}
