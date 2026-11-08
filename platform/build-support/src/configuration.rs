//! Finite, data-only schemas for generated Rust configuration.
//!
//! This module has no filesystem, environment, package discovery or device policy.
//! Callers supply an admitted device, its binding and normalized resources. Schema
//! version 1 emits scalar constants, enums and flat named-record constants. Rust
//! identifiers come only from validated package metadata; profile values are data.
//! The usize vocabulary is bounded to u32::MAX for the supported 32/64-bit targets.
use crate::Result;
use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

const MAX_SCHEMA_BYTES: usize = 64 * 1024;
const MAX_DECLARATIONS: usize = 64;
const MAX_FIELDS: usize = 128;
const MAX_VARIANTS: usize = 128;
const MAX_VALUE_BYTES: usize = 4096;
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
const MAX_PATH_DEPTH: usize = 8;
const MAX_KEY_BYTES: usize = 128;

/// Data for one selected device. All four values must be JSON objects.
pub struct Context<'a> {
    pub profile: &'a Value,
    pub device: &'a Value,
    pub resources: &'a Value,
    pub binding: &'a Value,
}

/// A validated package-owned schema. Construct it through Schema::parse.
#[derive(Debug)]
pub struct Schema {
    version: u32,
    output: String,
    role: String,
    enums: BTreeMap<String, BTreeMap<String, String>>,
    constants: BTreeMap<String, Field>,
    records: BTreeMap<String, Record>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SchemaInput {
    version: u32,
    output: String,
    role: String,
    #[serde(default)]
    enums: BTreeMap<String, BTreeMap<String, String>>,
    #[serde(default)]
    constants: BTreeMap<String, Field>,
    #[serde(default)]
    records: BTreeMap<String, Record>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Record {
    #[serde(rename = "type")]
    ty: String,
    #[serde(default)]
    static_lifetime: bool,
    fields: BTreeMap<String, Field>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Field {
    #[serde(rename = "type")]
    ty: String,
    source: Source,
    #[serde(default)]
    optional: bool,
    default: Option<Value>,
    min: Option<u64>,
    max: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Source {
    root: Root,
    path: Vec<String>,
    reference: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Root {
    Profile,
    Device,
    Binding,
    Resource,
}

impl Schema {
    pub fn parse(source: &str) -> Result<Self> {
        if source.len() > MAX_SCHEMA_BYTES {
            return Err("configuration schema exceeds 65536 bytes".into());
        }
        let input: SchemaInput =
            toml::from_str(source).map_err(|e| format!("invalid configuration schema: {e}"))?;
        let schema = Self {
            version: input.version,
            output: input.output,
            role: input.role,
            enums: input.enums,
            constants: input.constants,
            records: input.records,
        };
        schema.validate()?;
        Ok(schema)
    }

    pub fn output(&self) -> &str {
        &self.output
    }

    pub fn role(&self) -> &str {
        &self.role
    }

    fn validate(&self) -> Result<()> {
        if self.version != 1 {
            return Err(format!(
                "unsupported configuration schema version {}",
                self.version
            ));
        }
        let stem = self
            .output
            .strip_suffix(".rs")
            .ok_or("configuration output must be a Rust filename")?;
        if stem.is_empty()
            || stem.len() > 64
            || !stem
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        {
            return Err("configuration output must be a simple package-local filename".into());
        }
        validate_key(&self.role, "configuration role")?;
        let declarations = self.enums.len() + self.constants.len() + self.records.len();
        if declarations > MAX_DECLARATIONS || self.constants.is_empty() && self.records.is_empty() {
            return Err("configuration requires outputs and at most 64 declarations".into());
        }
        let mut names = BTreeSet::new();
        let mut variants = 0;
        for (name, choices) in &self.enums {
            validate_named_type(name)?;
            names.insert(name.as_str());
            if choices.is_empty() {
                return Err(format!("enum {name} requires at least one variant"));
            }
            variants += choices.len();
            let mut rust_names = BTreeSet::new();
            for (label, variant) in choices {
                validate_key(label, "enum input label")?;
                validate_identifier(variant)?;
                if !rust_names.insert(variant) {
                    return Err(format!("enum {name} repeats Rust variant {variant}"));
                }
            }
        }
        if variants > MAX_VARIANTS {
            return Err("configuration exceeds 128 enum variants".into());
        }
        let mut fields = self.constants.len();
        for (name, field) in &self.constants {
            validate_identifier(name)?;
            if !names.insert(name.as_str()) {
                return Err(format!("duplicate configuration declaration {name}"));
            }
            field.validate(self, name)?;
        }
        for (name, record) in &self.records {
            validate_identifier(name)?;
            if !names.insert(name.as_str()) {
                return Err(format!("duplicate configuration declaration {name}"));
            }
            validate_named_type(&record.ty)?;
            if self.enums.contains_key(&record.ty) {
                return Err(format!("record {name} cannot use an enum as its type"));
            }
            if record.fields.is_empty() {
                return Err(format!("record {name} requires fields"));
            }
            fields += record.fields.len();
            for (key, field) in &record.fields {
                validate_identifier(key)?;
                field.validate(self, &format!("{name}.{key}"))?;
            }
        }
        if fields > MAX_FIELDS {
            return Err("configuration exceeds 128 value fields".into());
        }
        Ok(())
    }
}

/// Render only declarations from the finite schema; values never become code.
pub fn render(schema: &Schema, context: &Context<'_>) -> Result<String> {
    for (name, value) in [
        ("profile", context.profile),
        ("device", context.device),
        ("resources", context.resources),
        ("binding", context.binding),
    ] {
        if !value.is_object() {
            return Err(format!("configuration context {name} must be an object"));
        }
    }
    let mut output = String::new();
    append(&mut output, "// Generated from a validated configuration schema.\n")?;
    for (name, choices) in &schema.enums {
        append(
            &mut output,
            &format!("#[allow(dead_code)]\nenum {name} {{\n"),
        )?;
        for variant in choices.values() {
            append(&mut output, &format!("    {variant},\n"))?;
        }
        append(&mut output, "}\n")?;
    }
    for (name, field) in &schema.constants {
        let (ty, value) = field.render(schema, context, name)?;
        append(&mut output, &format!("const {name}: {ty} = {value};\n"))?;
    }
    for (name, record) in &schema.records {
        let lifetime = if record.static_lifetime { "<'static>" } else { "" };
        append(
            &mut output,
            &format!(
                "const {name}: {}{lifetime} = {} {{\n",
                record.ty, record.ty
            ),
        )?;
        for (key, field) in &record.fields {
            let (_, value) = field.render(schema, context, &format!("{name}.{key}"))?;
            append(&mut output, &format!("    {key}: {value},\n"))?;
        }
        append(&mut output, "};\n")?;
    }
    Ok(output)
}

impl Field {
    fn validate(&self, schema: &Schema, label: &str) -> Result<()> {
        if !matches!(self.ty.as_str(), "str" | "bool")
            && numeric_limit(&self.ty).is_none()
            && !schema.enums.contains_key(&self.ty)
        {
            return Err(format!("{label}: unknown scalar or enum type {}", self.ty));
        }
        self.source.validate(label)?;
        if let Some(limit) = numeric_limit(&self.ty) {
            if self.min.is_some_and(|min| min > limit)
                || self.max.is_some_and(|max| max > limit)
                || self
                    .min
                    .zip(self.max)
                    .is_some_and(|(min, max)| min > max)
            {
                return Err(format!("{label}: invalid bounds for {}", self.ty));
            }
        } else if self.min.is_some() || self.max.is_some() {
            return Err(format!("{label}: bounds require an integer type"));
        }
        if let Some(default) = &self.default {
            self.literal(schema, default, label)?;
        }
        Ok(())
    }

    fn rust_type(&self) -> String {
        let ty = if self.ty == "str" { "&str" } else { &self.ty };
        if self.optional {
            format!("Option<{ty}>")
        } else {
            ty.to_owned()
        }
    }

    fn render(
        &self,
        schema: &Schema,
        context: &Context<'_>,
        label: &str,
    ) -> Result<(String, String)> {
        let selected = self.source.lookup(context, label)?.or(self.default.as_ref());
        let Some(value) = selected else {
            return if self.optional {
                Ok((self.rust_type(), "None".into()))
            } else {
                Err(format!("{label}: required configuration value is missing"))
            };
        };
        let value = self.literal(schema, value, label)?;
        Ok((
            self.rust_type(),
            if self.optional {
                format!("Some({value})")
            } else {
                value
            },
        ))
    }

    fn literal(&self, schema: &Schema, value: &Value, label: &str) -> Result<String> {
        match self.ty.as_str() {
            "str" => {
                let value = value
                    .as_str()
                    .ok_or_else(|| format!("{label}: expected a string"))?;
                if value.len() > MAX_VALUE_BYTES {
                    return Err(format!("{label}: string exceeds 4096 bytes"));
                }
                Ok(format!("{value:?}"))
            }
            "bool" => value
                .as_bool()
                .map(|value| value.to_string())
                .ok_or_else(|| format!("{label}: expected a boolean")),
            ty if numeric_limit(ty).is_some() => {
                let value = value
                    .as_u64()
                    .ok_or_else(|| format!("{label}: expected an unsigned integer"))?;
                let limit = numeric_limit(ty).ok_or("unknown integer type")?;
                if value < self.min.unwrap_or(0) || value > self.max.unwrap_or(limit) {
                    return Err(format!("{label}: integer is outside the bounds for {ty}"));
                }
                Ok(value.to_string())
            }
            ty => {
                let value = value
                    .as_str()
                    .ok_or_else(|| format!("{label}: expected an enum input label"))?;
                validate_key(value, label)?;
                let variant = schema
                    .enums
                    .get(ty)
                    .and_then(|choices| choices.get(value))
                    .ok_or_else(|| format!("{label}: no allowed {ty} variant for {value:?}"))?;
                Ok(format!("{ty}::{variant}"))
            }
        }
    }
}

impl Source {
    fn validate(&self, label: &str) -> Result<()> {
        validate_path(&self.path, label)?;
        match (self.root, &self.reference) {
            (Root::Resource, Some(path)) => validate_path(path, label),
            (Root::Resource, None) => {
                Err(format!("{label}: resource source requires a device reference"))
            }
            (_, Some(_)) => Err(format!("{label}: only resource sources accept a reference")),
            (_, None) => Ok(()),
        }
    }

    fn lookup<'a>(&self, context: &Context<'a>, label: &str) -> Result<Option<&'a Value>> {
        let root = match self.root {
            Root::Profile => context.profile,
            Root::Device => context.device,
            Root::Binding => context.binding,
            Root::Resource => {
                let path = self
                    .reference
                    .as_deref()
                    .ok_or("resource projection has no reference")?;
                let Some(reference) = lookup(context.device, path, label)? else {
                    return Ok(None);
                };
                let reference = reference
                    .as_str()
                    .ok_or_else(|| format!("{label}: resource reference must be a string"))?;
                validate_key(reference, label)?;
                let resource = context.resources.get(reference).ok_or_else(|| {
                    format!("{label}: referenced resource {reference:?} does not exist")
                })?;
                // An absent optional reference is allowed. A configured resource
                // with missing fields must fail rather than silently become None.
                return lookup(resource, &self.path, label)?.map(Some).ok_or_else(|| {
                    format!("{label}: referenced resource is missing its required property")
                });
            }
        };
        lookup(root, &self.path, label)
    }
}

fn lookup<'a>(root: &'a Value, path: &[String], label: &str) -> Result<Option<&'a Value>> {
    let mut value = root;
    for key in path {
        let object = value
            .as_object()
            .ok_or_else(|| format!("{label}: projection crosses a non-object value"))?;
        let Some(next) = object.get(key) else {
            return Ok(None);
        };
        value = next;
    }
    Ok(Some(value))
}

fn numeric_limit(ty: &str) -> Option<u64> {
    match ty {
        "u8" => Some(u64::from(u8::MAX)),
        "u16" => Some(u64::from(u16::MAX)),
        "u32" | "usize" => Some(u64::from(u32::MAX)),
        "u64" => Some(u64::MAX),
        _ => None,
    }
}

fn validate_named_type(value: &str) -> Result<()> {
    validate_identifier(value)?;
    if matches!(value, "str" | "bool") || numeric_limit(value).is_some() {
        return Err(format!("named type {value} collides with a scalar type"));
    }
    Ok(())
}

fn validate_identifier(value: &str) -> Result<()> {
    const KEYWORDS: &[&str] = &[
        "Self", "abstract", "as", "async", "await", "become", "box", "break", "const",
        "continue", "crate", "do", "dyn", "else", "enum", "extern", "false", "final",
        "fn", "for", "gen", "if", "impl", "in", "let", "loop", "macro", "macro_rules",
        "match", "mod", "move", "mut", "override", "priv", "pub", "raw", "ref",
        "return", "safe", "self", "static", "struct", "super", "trait", "true",
        "try", "type", "typeof", "union", "unsafe", "unsized", "use", "virtual",
        "where", "while", "yield",
    ];
    let mut bytes = value.bytes();
    let first = bytes.next();
    if value.len() > 64
        || value == "_"
        || !first.is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        || !bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
        || KEYWORDS.contains(&value)
    {
        return Err(format!("invalid Rust configuration identifier {value:?}"));
    }
    Ok(())
}

fn validate_key(value: &str, label: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > MAX_KEY_BYTES
        || value.chars().any(char::is_control)
    {
        return Err(format!("{label}: invalid or oversized input key"));
    }
    Ok(())
}

fn validate_path(path: &[String], label: &str) -> Result<()> {
    if path.is_empty() || path.len() > MAX_PATH_DEPTH {
        return Err(format!("{label}: projection requires one to eight keys"));
    }
    for key in path {
        validate_key(key, label)?;
    }
    Ok(())
}

fn append(output: &mut String, value: &str) -> Result<()> {
    if output.len().saturating_add(value.len()) > MAX_OUTPUT_BYTES {
        return Err("generated configuration exceeds 1048576 bytes".into());
    }
    output.push_str(value);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SCHEMA: &str = r#"
version = 1
output = "device_config.rs"
role = "device"

[enums.Kind]
alpha = "Alpha"
beta = "Beta"

[constants.KIND]
type = "Kind"
source = { root = "binding", path = ["model"] }

[constants.STACK_BYTES]
type = "usize"
source = { root = "profile", path = ["budget", "stack-bytes"] }
default = 16384
min = 8192
max = 262144

[records.BINDING]
type = "ResourceBinding"
static-lifetime = true

[records.BINDING.fields.path]
type = "str"
source = { root = "resource", reference = ["bus"], path = ["device"] }

[records.BINDING.fields.address]
type = "u8"
source = { root = "device", path = ["address"] }

[records.BINDING.fields.irq_path]
type = "str"
optional = true
source = { root = "resource", reference = ["wake", "resource"], path = ["device"] }
"#;

    struct Input {
        profile: Value,
        device: Value,
        resources: Value,
        binding: Value,
    }

    impl Input {
        fn new() -> Self {
            Self {
                profile: json!({}),
                device: json!({
                    "bus": "sensor_bus",
                    "address": 104,
                    "wake": { "kind": "periodic" },
                }),
                resources: json!({
                    "sensor_bus": { "kind": "i2c", "device": "/dev/i2c0" },
                    "interrupt": { "kind": "gpio-interrupt", "device": "/dev/gpio0" },
                }),
                binding: json!({ "model": "alpha" }),
            }
        }

        fn context(&self) -> Context<'_> {
            Context {
                profile: &self.profile,
                device: &self.device,
                resources: &self.resources,
                binding: &self.binding,
            }
        }
    }

    fn scalar_schema(ty: &str) -> String {
        format!(
            "version = 1\noutput = \"value.rs\"\nrole = \"device\"\n\
             [constants.VALUE]\ntype = \"{ty}\"\n\
             source = {{ root = \"profile\", path = [\"value\"] }}\n"
        )
    }

    #[test]
    fn renders_selected_data_defaults_and_optional_resource() {
        let schema = Schema::parse(SCHEMA).unwrap();
        assert_eq!(schema.output(), "device_config.rs");
        assert_eq!(schema.role(), "device");
        let mut input = Input::new();
        let generated = render(&schema, &input.context()).unwrap();
        assert!(generated.contains("const KIND: Kind = Kind::Alpha;"));
        assert!(generated.contains("const STACK_BYTES: usize = 16384;"));
        assert!(generated.contains("const BINDING: ResourceBinding<'static> = ResourceBinding {"));
        assert!(generated.contains("    address: 104,"));
        assert!(generated.contains("    irq_path: None,"));
        assert!(generated.contains("    path: \"/dev/i2c0\","));

        input.profile = json!({ "budget": { "stack-bytes": 32768 } });
        input.device["wake"] = json!({ "kind": "interrupt", "resource": "interrupt" });
        input.binding["model"] = json!("beta");
        let generated = render(&schema, &input.context()).unwrap();
        assert!(generated.contains("const KIND: Kind = Kind::Beta;"));
        assert!(generated.contains("const STACK_BYTES: usize = 32768;"));
        assert!(generated.contains("    irq_path: Some(\"/dev/gpio0\"),"));
        assert_eq!(generated, render(&schema, &input.context()).unwrap());
    }

    #[test]
    fn rejects_missing_required_malformed_and_unknown_enum_values() {
        let schema = Schema::parse(SCHEMA).unwrap();
        let mut input = Input::new();
        input.device.as_object_mut().unwrap().remove("address");
        assert!(render(&schema, &input.context()).unwrap_err().contains("missing"));
        input.device["address"] = json!(104);
        input.binding["model"] = json!("unlisted");
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("no allowed Kind variant"));
        input.binding["model"] = json!("alpha");
        input.profile = json!({ "budget": { "stack-bytes": null } });
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("unsigned integer"));
        input.profile = json!({ "budget": "not an object" });
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("non-object"));
    }

    #[test]
    fn configured_optional_resource_cannot_silently_disappear() {
        let schema = Schema::parse(SCHEMA).unwrap();
        let mut input = Input::new();
        input.device["wake"] = json!({ "resource": "missing" });
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("does not exist"));
        input.device["wake"] = json!({ "resource": "interrupt" });
        input.resources["interrupt"] = json!({ "kind": "gpio-interrupt" });
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("missing its required property"));
        input.device["wake"] = json!({ "resource": 1 });
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("reference must be a string"));
    }

    #[test]
    fn optional_default_applies_only_to_absent_values() {
        let source = scalar_schema("u8") + "optional = true\ndefault = 7\n";
        let schema = Schema::parse(&source).unwrap();
        let mut input = Input::new();
        assert!(render(&schema, &input.context())
            .unwrap()
            .contains("const VALUE: Option<u8> = Some(7);"));
        input.profile["value"] = json!(0);
        assert!(render(&schema, &input.context())
            .unwrap()
            .contains("const VALUE: Option<u8> = Some(0);"));
        input.profile["value"] = Value::Null;
        assert!(render(&schema, &input.context()).is_err());
    }

    #[test]
    fn checks_integer_widths_budgets_and_invalid_defaults() {
        let mut input = Input::new();
        for (ty, maximum) in [
            ("u8", u64::from(u8::MAX)),
            ("u16", u64::from(u16::MAX)),
            ("u32", u64::from(u32::MAX)),
            ("usize", u64::from(u32::MAX)),
            ("u64", u64::MAX),
        ] {
            let schema = Schema::parse(&scalar_schema(ty)).unwrap();
            input.profile["value"] = json!(maximum);
            assert!(render(&schema, &input.context()).is_ok());
            if let Some(too_large) = maximum.checked_add(1) {
                input.profile["value"] = json!(too_large);
                assert!(render(&schema, &input.context()).is_err());
            }
            for invalid in [json!(-1), json!(1.0), json!("1"), json!(true)] {
                input.profile["value"] = invalid;
                assert!(render(&schema, &input.context()).is_err());
            }
        }
        let schema = Schema::parse(SCHEMA).unwrap();
        input.profile = json!({ "budget": { "stack-bytes": 8191 } });
        assert!(render(&schema, &input.context()).is_err());
        input.profile = json!({ "budget": { "stack-bytes": 262145 } });
        assert!(render(&schema, &input.context()).is_err());
        assert!(Schema::parse(&(scalar_schema("u8") + "default = 256\n")).is_err());
        assert!(Schema::parse(&(scalar_schema("u8") + "min = 9\nmax = 8\n")).is_err());
        assert!(Schema::parse(&(scalar_schema("str") + "min = 1\n")).is_err());
    }

    #[test]
    fn validates_identifiers_types_and_projection_shape() {
        for invalid in ["type", "Self", "_", "x::y", "x; panic!()", "1field", "r#type"] {
            let source = scalar_schema("u8")
                .replace("constants.VALUE", &format!("constants.{invalid:?}"));
            assert!(Schema::parse(&source).is_err(), "{invalid}");
        }
        assert!(Schema::parse(&SCHEMA.replace("ResourceBinding", "module::Binding")).is_err());
        assert!(Schema::parse(&SCHEMA.replace("Alpha", "Alpha; panic!()")).is_err());
        assert!(Schema::parse(&scalar_schema("std::string::String")).is_err());
        assert!(Schema::parse(
            &scalar_schema("u8").replace("path = [\"value\"]", "path = []")
        )
        .is_err());
        assert!(Schema::parse(
            &scalar_schema("u8").replace(
                "root = \"profile\"",
                "root = \"profile\", reference = [\"bus\"]"
            )
        )
        .is_err());
        assert!(Schema::parse(
            &scalar_schema("u8").replace("root = \"profile\"", "root = \"resource\"")
        )
        .is_err());
        assert!(Schema::parse(&scalar_schema("u8").replace("value.rs", "../value.rs")).is_err());
    }

    #[test]
    fn rejects_duplicate_declarations_variants_and_fields() {
        let duplicate_output = SCHEMA.to_owned()
            + "\n[constants.BINDING]\ntype = \"u8\"\n\
               source = { root = \"device\", path = [\"address\"] }\n";
        assert!(Schema::parse(&duplicate_output)
            .unwrap_err()
            .contains("duplicate configuration declaration"));
        assert!(Schema::parse(&SCHEMA.replace("beta = \"Beta\"", "beta = \"Alpha\""))
            .unwrap_err()
            .contains("repeats Rust variant"));
        assert!(Schema::parse(&(scalar_schema("u8") + "type = \"u16\"\n")).is_err());
        assert!(Schema::parse(&(scalar_schema("u8") + "rust = \"arbitrary code\"\n")).is_err());
    }

    #[test]
    fn bounded_schema_values_and_generated_output() {
        assert!(Schema::parse(&" ".repeat(MAX_SCHEMA_BYTES + 1)).is_err());
        let schema = Schema::parse(&scalar_schema("str")).unwrap();
        let mut input = Input::new();
        input.profile["value"] = json!("x".repeat(MAX_VALUE_BYTES + 1));
        assert!(render(&schema, &input.context()).is_err());

        let mut source = "version = 1\noutput = \"value.rs\"\nrole = \"device\"\n".to_owned();
        for index in 0..MAX_DECLARATIONS {
            source.push_str(&format!(
                "[constants.VALUE_{index}]\ntype = \"str\"\n\
                 source = {{ root = \"profile\", path = [\"value\"] }}\n"
            ));
        }
        let schema = Schema::parse(&source).unwrap();
        input.profile["value"] = json!("\u{1}".repeat(MAX_VALUE_BYTES));
        assert!(render(&schema, &input.context())
            .unwrap_err()
            .contains("generated configuration exceeds"));
    }

    #[test]
    fn generated_string_literals_compile_and_preserve_bytes() {
        let schema = Schema::parse(&scalar_schema("str")).unwrap();
        let mut input = Input::new();
        let value = "quote\"; panic!(\"not code\"); //\n\\\0\r\t\u{1}\u{2028}🦀";
        input.profile["value"] = json!(value);
        let generated = render(&schema, &input.context()).unwrap();
        let bytes = value
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte}u8"))
            .collect::<Vec<_>>()
            .join(", ");
        let temporary = crate::TempDir::new(&std::env::temp_dir()).unwrap();
        let source = temporary.0.join("literal.rs");
        let binary = temporary.0.join(if cfg!(windows) {
            "literal.exe"
        } else {
            "literal"
        });
        std::fs::write(
            &source,
            format!("{generated}\nfn main() {{ assert_eq!(VALUE.as_bytes(), &[{bytes}]); }}\n"),
        )
        .unwrap();
        let compiler = std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
        let compiled = std::process::Command::new(compiler)
            .args(["--edition", "2021"])
            .arg(&source)
            .arg("-o")
            .arg(&binary)
            .output()
            .unwrap();
        assert!(
            compiled.status.success(),
            "{}",
            String::from_utf8_lossy(&compiled.stderr)
        );
        let executed = std::process::Command::new(&binary).output().unwrap();
        assert!(
            executed.status.success(),
            "{}",
            String::from_utf8_lossy(&executed.stderr)
        );
    }
}
