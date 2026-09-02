import Form from "@rjsf/mui";
import validator from "@rjsf/validator-cfworker";
import type { RJSFSchema } from "@rjsf/utils";
import { Alert, Box, Button, Card, CircularProgress, Stack, Typography } from "@mui/material";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { executeRequest } from "../lib/runtime";
import { getPointer, interpolate } from "../lib/pointer";
import type { FormPageSpec, ModuleManifest, PageManifest } from "../types";
import { PageFrame } from "./PageFrame";

function cloneSchema(schema: RJSFSchema): RJSFSchema {
  return structuredClone(schema);
}

export function FormPage({ module, page }: { module: ModuleManifest; page: PageManifest }) {
  const spec = page as FormPageSpec;
  const navigate = useNavigate();
  const [schema, setSchema] = useState<RJSFSchema>(() => cloneSchema(spec.schema));
  const [optionsLoading, setOptionsLoading] = useState(Boolean(spec.dynamicOptions?.length));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>();
  const initialData = useMemo(() => {
    const data: Record<string, unknown> = {};
    const properties = spec.schema.properties || {};
    Object.entries(properties).forEach(([name, definition]) => {
      if (definition && typeof definition === "object" && "default" in definition) data[name] = definition.default;
    });
    return data;
  }, [spec.schema]);

  useEffect(() => {
    let active = true;
    async function loadOptions() {
      const next = cloneSchema(spec.schema);
      try {
        for (const dynamic of spec.dynamicOptions || []) {
          const response = await executeRequest(dynamic.request, module.dataSources, {});
          const items = getPointer(response, dynamic.itemsPointer);
          if (!Array.isArray(items)) throw new Error(`Options for ${dynamic.field} are not an array`);
          const property = next.properties?.[dynamic.field];
          if (!property || typeof property === "boolean") throw new Error(`Unknown form field: ${dynamic.field}`);
          property.oneOf = items.map((item) => ({
            const: getPointer(item, dynamic.valuePointer) as string,
            title: String(getPointer(item, dynamic.labelPointer) ?? getPointer(item, dynamic.valuePointer)),
          }));
        }
        if (active) setSchema(next);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Unable to load form options");
      } finally {
        if (active) setOptionsLoading(false);
      }
    }
    void loadOptions();
    return () => { active = false; };
  }, [module.dataSources, spec.dynamicOptions, spec.schema]);

  return (
    <PageFrame back="/tibuild/builds" eyebrow={module.title} title={page.title} description={page.description}>
      <Card variant="outlined" sx={{ maxWidth: 760, p: { xs: 2, sm: 3 } }}>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        {optionsLoading ? <Box className="center-state"><CircularProgress size={28} /><Typography>Loading build options…</Typography></Box> : (
          <Form
            schema={schema}
            uiSchema={spec.uiSchema}
            validator={validator}
            initialFormData={initialData}
            disabled={submitting}
            focusOnFirstError
            onSubmit={async ({ formData }) => {
              setSubmitting(true);
              setError(undefined);
              try {
                const result = await executeRequest(spec.submit.request, module.dataSources, { form: formData });
                const id = getPointer(result, spec.submit.resultIDPointer);
                navigate(interpolate(spec.submit.successNavigate, { id }));
              } catch (err) {
                setError(err instanceof Error ? err.message : "Unable to create build");
              } finally {
                setSubmitting(false);
              }
            }}
          >
            <Stack direction="row" sx={{ justifyContent: "flex-end", gap: 1, mt: 3 }}>
              <Button color="inherit" onClick={() => navigate("/tibuild/builds")}>Cancel</Button>
              <Button type="submit" variant="contained" disabled={submitting} startIcon={submitting ? <CircularProgress color="inherit" size={16} /> : undefined}>
                {submitting ? "Starting…" : spec.submit.label}
              </Button>
            </Stack>
          </Form>
        )}
      </Card>
    </PageFrame>
  );
}
