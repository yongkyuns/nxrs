//! Ordered, syntactic profile graph merging.
//!
//! This module deliberately does not validate the resulting device graph. It
//! only checks that each input has a mergeable shape and preserves enough
//! source information for the final graph validator to report useful errors.

use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

pub(crate) type Origins = BTreeMap<String, PathBuf>;

const GRAPH_TABLES: &[&str] = &["resources", "devices", "roles"];
pub(crate) const NATIVE_ENDPOINT_TABLES: &[&str] = &["camera", "ble"];
const REMOVALS: &[(&str, &str)] = &[
    ("remove-devices", "devices"),
    ("remove-resources", "resources"),
    ("remove-roles", "roles"),
];

/// Merge one profile graph or explicit overlay into `data`.
///
/// Merge graph tables and native endpoint bindings. Explicit overlays may also
/// remove graph records.
pub(crate) fn merge(
    data: &mut Value,
    input: &Value,
    path: &Path,
    origins: &mut Origins,
    overlay: bool,
) -> crate::Result<()> {
    let input_table = input
        .as_object()
        .ok_or_else(|| error(path, "<root>", "expected a table"))?;

    if overlay {
        for key in input_table.keys() {
            if !GRAPH_TABLES.contains(&key.as_str())
                && !NATIVE_ENDPOINT_TABLES.contains(&key.as_str())
                && !REMOVALS.iter().any(|(remove, _)| remove == key)
            {
                return Err(error(path, key, "field is not allowed in an overlay"));
            }
        }
    }

    if !data.is_object() {
        return Err(error(path, "<root>", "destination must be a table"));
    }

    for table in GRAPH_TABLES {
        let Some(incoming) = input_table.get(*table) else {
            continue;
        };
        merge_table(data, incoming, table, path, origins)?;
    }
    for &table in NATIVE_ENDPOINT_TABLES {
        if let Some(incoming) = input_table.get(table) {
            data[table] = incoming.clone();
            clear_origin_tree(origins, table);
            note_tree(origins, table, incoming, path);
        }
    }

    if overlay {
        for (remove_key, table) in REMOVALS {
            let Some(ids) = input_table.get(*remove_key) else {
                continue;
            };
            let ids = ids
                .as_array()
                .ok_or_else(|| error(path, remove_key, "expected an array of IDs"))?;
            let mut seen = BTreeSet::new();
            for (index, id) in ids.iter().enumerate() {
                let id = id.as_str().ok_or_else(|| {
                    error(
                        path,
                        &format!("{remove_key}[{index}]"),
                        "expected a string ID",
                    )
                })?;
                if !seen.insert(id) {
                    return Err(error(
                        path,
                        remove_key,
                        &format!("duplicate removal ID '{id}'"),
                    ));
                }
                if input_table
                    .get(*table)
                    .and_then(Value::as_object)
                    .is_some_and(|updates| updates.contains_key(id))
                {
                    return Err(error(
                        path,
                        &format!("{table}.{id}"),
                        "the same overlay removes and updates this ID",
                    ));
                }
                let removed = data
                    .get_mut(*table)
                    .and_then(Value::as_object_mut)
                    .is_some_and(|target| target.remove(id).is_some());
                if !removed {
                    return Err(error(
                        path,
                        &format!("{remove_key}[{index}]"),
                        &format!("unknown {table} ID '{id}'"),
                    ));
                }
                let field = format!("{table}.{id}");
                clear_origin_tree(origins, &field);
                // Preserve a tombstone so later dangling-reference checks can
                // point to the input that removed the record.
                origins.insert(field, path.to_path_buf());
            }
        }
    }

    Ok(())
}

fn merge_table(
    data: &mut Value,
    incoming: &Value,
    table: &str,
    path: &Path,
    origins: &mut Origins,
) -> crate::Result<()> {
    let incoming = incoming
        .as_object()
        .ok_or_else(|| error(path, table, "expected a table keyed by ID"))?;
    if data.get(table).is_none() {
        data[table] = Value::Object(Default::default());
    }
    let target = data
        .get_mut(table)
        .and_then(Value::as_object_mut)
        .ok_or_else(|| error(path, table, "destination must be a table keyed by ID"))?;

    for (id, record) in incoming {
        let field = format!("{table}.{id}");
        if table == "roles" {
            if !record.is_string() {
                return Err(error(path, &field, "role assignment must be a string ID"));
            }
            target.insert(id.clone(), record.clone());
            origins.insert(field, path.to_path_buf());
            continue;
        }
        if !record.is_object() {
            return Err(error(path, &field, "record must be a table"));
        }

        let existing = target.get(id);
        let replacement = existing.is_some_and(|old| record_replaces(old, record, table));
        if replacement || existing.is_none() {
            clear_origin_tree(origins, &field);
            target.insert(id.clone(), record.clone());
            note_tree(origins, &field, record, path);
        } else {
            let current = target.get_mut(id).expect("existing record");
            merge_record(current, record, &field, path, origins);
        }
    }
    Ok(())
}

fn record_replaces(old: &Value, new: &Value, table: &str) -> bool {
    let discriminator = if table == "devices" {
        "compatible"
    } else {
        "kind"
    };
    old.get(discriminator)
        .zip(new.get(discriminator))
        .is_some_and(|(before, after)| before != after)
}

fn merge_record(
    target: &mut Value,
    incoming: &Value,
    field: &str,
    source: &Path,
    origins: &mut Origins,
) {
    origins.insert(field.to_owned(), source.to_path_buf());
    let (Some(target), Some(incoming)) = (target.as_object_mut(), incoming.as_object()) else {
        return;
    };
    for (key, value) in incoming {
        let property = format!("{field}.{key}");
        clear_origin_tree(origins, &property);
        target.insert(key.clone(), value.clone());
        note_tree(origins, &property, value, source);
    }
}

fn note_tree(origins: &mut Origins, field: &str, value: &Value, source: &Path) {
    origins.insert(field.to_owned(), source.to_path_buf());
    if let Some(object) = value.as_object() {
        for (key, child) in object {
            note_tree(origins, &format!("{field}.{key}"), child, source);
        }
    }
}

fn clear_origin_tree(origins: &mut Origins, field: &str) {
    let prefix = format!("{field}.");
    origins.retain(|key, _| key != field && !key.starts_with(&prefix));
}

fn error(path: &Path, property: &str, message: &str) -> String {
    format!("{}: {property}: {message}", path.display())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn source(s: &str) -> PathBuf {
        PathBuf::from(s)
    }

    #[test]
    fn merges_properties_in_order_and_tracks_last_contributor() {
        let mut data = json!({"devices":{"motion":{"compatible":"bosch,bmi270","bus":"i2c","address":104,"wake":{"kind":"periodic"}}}});
        let mut origins = Origins::new();
        merge(&mut data, &json!({"devices":{"motion":{"address":105,"wake":{"kind":"interrupt","resource":"irq"}}}}), Path::new("first.toml"), &mut origins, true).unwrap();
        merge(
            &mut data,
            &json!({"devices":{"motion":{"address":106}}}),
            Path::new("second.toml"),
            &mut origins,
            true,
        )
        .unwrap();
        assert_eq!(data["devices"]["motion"]["address"], 106);
        assert_eq!(data["devices"]["motion"]["wake"]["resource"], "irq");
        assert_eq!(origins["devices.motion.address"], source("second.toml"));
        assert_eq!(origins["devices.motion.wake"], source("first.toml"));
    }

    #[test]
    fn discriminator_changes_replace_records_and_clear_stale_properties() {
        let mut data = json!({"resources":{"bus":{"kind":"i2c","device":"/dev/i2c0","frequency":400000}},"devices":{"motion":{"compatible":"old,sensor","bus":"bus","address":104,"wake":{"kind":"interrupt","resource":"irq"}}}});
        let mut origins = Origins::new();
        merge(&mut data, &json!({"resources":{"bus":{"kind":"spi","device":"/dev/spi0"}},"devices":{"motion":{"compatible":"new,sensor","bus":"bus","address":1,"wake":{"kind":"periodic"}}}}), Path::new("replacement.toml"), &mut origins, true).unwrap();
        assert_eq!(
            data["resources"]["bus"],
            json!({"kind":"spi","device":"/dev/spi0"})
        );
        assert_eq!(
            data["devices"]["motion"],
            json!({"compatible":"new,sensor","bus":"bus","address":1,"wake":{"kind":"periodic"}})
        );
        assert!(!origins.contains_key("devices.motion.wake.resource"));
        assert!(!origins.contains_key("resources.bus.frequency"));
        assert_eq!(
            origins["devices.motion.address"],
            source("replacement.toml")
        );
    }

    #[test]
    fn removals_create_tombstones_and_reject_unknown_duplicate_or_conflicting_ids() {
        let mut data = json!({"devices":{"motion":{"compatible":"x"}},"roles":{"imu":"motion"}});
        let mut origins = Origins::new();
        merge(
            &mut data,
            &json!({"remove-devices":["motion"]}),
            Path::new("remove.toml"),
            &mut origins,
            true,
        )
        .unwrap();
        assert_eq!(origins["devices.motion"], source("remove.toml"));
        assert!(merge(
            &mut data,
            &json!({"remove-devices":["motion"]}),
            Path::new("unknown.toml"),
            &mut origins,
            true
        )
        .unwrap_err()
        .contains("remove-devices[0]"));

        let mut data = json!({"devices":{"motion":{"compatible":"x"}}});
        assert!(merge(
            &mut data,
            &json!({"devices":{"motion":{"address":1}},"remove-devices":["motion"]}),
            Path::new("conflict.toml"),
            &mut origins,
            true
        )
        .unwrap_err()
        .contains("removes and updates"));
        assert!(merge(
            &mut data,
            &json!({"remove-devices":["motion","motion"]}),
            Path::new("duplicate.toml"),
            &mut origins,
            true
        )
        .unwrap_err()
        .contains("duplicate removal"));
    }

    #[test]
    fn overlays_reject_non_graph_settings_and_malformed_tables_with_locations() {
        let mut data = json!({});
        let mut origins = Origins::new();
        for key in [
            "include",
            "target-os",
            "board",
            "execution",
            "composition",
            "hal",
            "imu",
        ] {
            let error = merge(
                &mut data,
                &json!({key: {"x": 1}}),
                Path::new("bad.toml"),
                &mut origins,
                true,
            )
            .unwrap_err();
            assert!(error.contains("bad.toml") && error.contains(key), "{error}");
        }
        assert!(merge(
            &mut data,
            &json!({"devices":{"motion":1}}),
            Path::new("shape.toml"),
            &mut origins,
            true
        )
        .unwrap_err()
        .contains("devices.motion"));
    }

    #[test]
    fn inherited_profiles_only_merge_graph_tables() {
        let mut data = json!({"name":"parent","execution":"nuttx","roles":{"imu":"motion"}});
        let mut origins = Origins::new();
        merge(
            &mut data,
            &json!({"name":"child","execution":"std","devices":{"motion":{"compatible":"x"}}}),
            Path::new("child.toml"),
            &mut origins,
            false,
        )
        .unwrap();
        assert_eq!(data["name"], "parent");
        assert_eq!(data["execution"], "nuttx");
        assert_eq!(data["devices"]["motion"]["compatible"], "x");
    }
}
