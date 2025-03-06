import { assertEquals, assertThrows } from "jsr:@std/assert";
import { convertDsnToClientConfig } from "./prow-jobs.ts";

Deno.test("should correctly parse a valid DSN", () => {
  const dsn = "mysql://user:password@localhost:5432/database";
  const config = convertDsnToClientConfig(dsn);

  assertEquals(config, {
    hostname: "localhost",
    port: 5432,
    username: "user",
    password: "password",
    db: "database",
  });
});

Deno.test("should throw an error if DSN is missing user and password", () => {
  const dsn = "@localhost:5432/database";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should throw an error if DSN is missing host and port", () => {
  const dsn = "mysql://user:password@/database";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should throw an error if DSN is missing database", () => {
  const dsn = "mysql://user:password@localhost:5432/";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should correctly parse a DSN with special characters in user and password", () => {
  const dsn = "mysql://user%40name:pass%3Aword@localhost:5432/database";
  const config = convertDsnToClientConfig(dsn);

  assertEquals(config, {
    hostname: "localhost",
    port: 5432,
    username: "user@name",
    password: "pass:word",
    db: "database",
  });
});
