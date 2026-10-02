//! Precomputed pg_query tree experiment.
//!
//! Usage: pgtrees <in.jsonl> <out_dir>
//!
//! For every corpus record (`sql` field) it:
//! - parses with libpg_query's native JSON entry point (`pg_query_parse`), which
//!   writes enum values as names and omits zero ints, false bools, NULL strings
//!   and NULL lists;
//! - parses with the pg_query crate's protobuf entry point and serialises the
//!   prost structs through their derived `serde::Serialize` (for size and
//!   consistency comparison only);
//! - writes canonical JSON (keys sorted, compact) with every `location` key
//!   removed, plus the location values separately.

use std::collections::{BTreeMap, HashMap};
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::os::raw::{c_char, c_int};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

#[repr(C)]
struct PgQueryError {
	message: *mut c_char,
	funcname: *mut c_char,
	filename: *mut c_char,
	lineno: c_int,
	cursorpos: c_int,
	context: *mut c_char,
}

#[repr(C)]
struct PgQueryParseResult {
	parse_tree: *mut c_char,
	stderr_buffer: *mut c_char,
	error: *mut PgQueryError,
}

extern "C" {
	fn pg_query_parse(input: *const c_char) -> PgQueryParseResult;
	fn pg_query_free_parse_result(result: PgQueryParseResult);
}

enum Native {
	Ok(String),
	Err { msg: String, file: String, cursor: i32 },
}

fn native_parse(sql: &str) -> Option<Native> {
	let c = CString::new(sql).ok()?;
	unsafe {
		let r = pg_query_parse(c.as_ptr());
		let out = if !r.error.is_null() {
			let e = &*r.error;
			let msg = CStr::from_ptr(e.message).to_string_lossy().into_owned();
			let file = if e.filename.is_null() {
				String::new()
			} else {
				CStr::from_ptr(e.filename).to_string_lossy().into_owned()
			};
			Native::Err { msg, file, cursor: e.cursorpos }
		} else {
			Native::Ok(CStr::from_ptr(r.parse_tree).to_string_lossy().into_owned())
		};
		pg_query_free_parse_result(r);
		Some(out)
	}
}

fn stmt_id(sql: &str) -> String {
	let d = Sha256::digest(sql.as_bytes());
	hex::encode(&d[..8])
}

/// Pre-order walk in sorted-key order. Collects every `location` value, with
/// its JSON-pointer path, in the order the key appears in the sorted-key
/// serialisation of the unstripped tree.
fn collect_locs(v: &Value, path: &mut String, out: &mut Vec<(String, i64)>) {
	match v {
		Value::Object(m) => {
			for (k, child) in m {
				if is_loc_key(k) && child.is_i64() {
					out.push((format!("{path}#{k}"), child.as_i64().unwrap_or(i64::MIN)));
				} else {
					let len = path.len();
					path.push('/');
					path.push_str(k);
					collect_locs(child, path, out);
					path.truncate(len);
				}
			}
		}
		Value::Array(a) => {
			for (i, child) in a.iter().enumerate() {
				let len = path.len();
				path.push('/');
				path.push_str(&i.to_string());
				collect_locs(child, path, out);
				path.truncate(len);
			}
		}
		_ => {}
	}
}

/// Integer position fields in the raw parse tree (pg_query.proto: `location`,
/// `arg_location`, `conninfo_location`, `name_location`, `payload_location`).
/// `CreateTableSpaceStmt.location` is a string (the directory) and is kept.
fn is_loc_key(k: &str) -> bool {
	k == "location" || k.ends_with("_location")
}

fn strip(v: &mut Value, keys: &[&str]) {
	match v {
		Value::Object(m) => {
			for k in keys {
				if m.get(*k).is_some_and(|x| x.is_i64()) {
					m.remove(*k);
				}
			}
			let extra: Vec<String> = m.iter().filter(|(k, x)| is_loc_key(k) && x.is_i64()).map(|(k, _)| k.clone()).collect();
			for k in extra {
				m.remove(&k);
			}
			for (_, child) in m.iter_mut() {
				strip(child, keys);
			}
		}
		Value::Array(a) => {
			for child in a.iter_mut() {
				strip(child, keys);
			}
		}
		_ => {}
	}
}

fn count_key(v: &Value, key: &str, nonzero: bool) -> usize {
	match v {
		Value::Object(m) => m
			.iter()
			.map(|(k, c)| {
				let here = if k != "stmt_location" && (k == key || (key == "location" && is_loc_key(k))) && c.is_i64() && (!nonzero || c.as_i64() != Some(0)) { 1 } else { 0 };
				here + count_key(c, key, nonzero)
			})
			.sum(),
		Value::Array(a) => a.iter().map(|c| count_key(c, key, nonzero)).sum(),
		_ => 0,
	}
}

fn node_types(v: &Value, out: &mut BTreeMap<String, usize>) {
	match v {
		Value::Object(m) => {
			for (k, c) in m {
				if k.chars().next().is_some_and(|ch| ch.is_ascii_uppercase()) && c.is_object() {
					*out.entry(k.clone()).or_default() += 1;
				}
				node_types(c, out);
			}
		}
		Value::Array(a) => a.iter().for_each(|c| node_types(c, out)),
		_ => {}
	}
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	let input = &args[1];
	let out_dir = std::path::Path::new(&args[2]);
	std::fs::create_dir_all(out_dir).unwrap();
	let mk = |n: &str| BufWriter::new(File::create(out_dir.join(n)).unwrap());
	let mut w_trees = mk("trees_native.jsonl");
	let mut w_locs = mk("locs_native.jsonl");
	let mut w_locpaths = mk("locs_paths.jsonl");
	let mut w_prost = mk("trees_prost.jsonl");
	let mut w_rejects = mk("rejects.jsonl");
	let mut w_mismatch = mk("label_mismatch.jsonl");

	let mut seen: HashMap<String, String> = HashMap::new();
	let mut n_records = 0usize;
	let mut n_unique = 0usize;
	let mut n_dup = 0usize;
	let mut n_nul = 0usize;
	let mut n_accept = 0usize;
	let mut n_reject = 0usize;
	let mut n_ser_fail = 0usize;
	let mut n_loc_inconsistent = 0usize;
	let mut n_id_collision = 0usize;
	let mut labels: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
	let mut t_native = Duration::ZERO;
	let mut t_canon = Duration::ZERO;
	let mut t_prost = Duration::ZERO;
	let mut sizes: Vec<usize> = Vec::new();
	let mut nodes: BTreeMap<String, usize> = BTreeMap::new();
	let mut total_locs = 0usize;
	let mut neg_locs = 0usize;
	let t0 = Instant::now();

	for line in BufReader::new(File::open(input).unwrap()).lines() {
		let line = line.unwrap();
		let rec: Value = serde_json::from_str(&line).unwrap();
		n_records += 1;
		let sql = rec["sql"].as_str().unwrap().to_string();
		let label = rec["expected"].as_str().unwrap_or("?").to_string();
		let file = rec["file"].as_str().unwrap_or("?").to_string();
		let id = stmt_id(&sql);
		if let Some(prev) = seen.get(&id) {
			if prev != &sql {
				n_id_collision += 1;
			}
			n_dup += 1;
			continue;
		}
		seen.insert(id.clone(), sql.clone());
		n_unique += 1;

		let t = Instant::now();
		let Some(native) = native_parse(&sql) else {
			n_nul += 1;
			continue;
		};
		t_native += t.elapsed();

		match native {
			Native::Err { msg, file: src, cursor } => {
				n_reject += 1;
				*labels.entry(label.clone()).or_default().entry("pg17_reject".into()).or_default() += 1;
				let raw = matches!(src.as_str(), "scan.l" | "gram.y" | "parser.c" | "scansup.c");
				writeln!(
					w_rejects,
					"{}",
					json!({"id": id, "file": file, "label": label, "src": src, "raw_parser": raw, "cursor": cursor, "msg": msg})
				)
				.unwrap();
				if label == "ok" {
					writeln!(w_mismatch, "{}", json!({"id": id, "file": file, "kind": "label_ok_pg17_reject", "msg": msg}))
						.unwrap();
				}
			}
			Native::Ok(text) => {
				let t = Instant::now();
				let parsed: Result<Value, _> = serde_json::from_str(&text);
				let Ok(root) = parsed else {
					n_ser_fail += 1;
					continue;
				};
				let stmts = root.get("stmts").and_then(|s| s.as_array()).cloned().unwrap_or_default();
				let mut split = Vec::new();
				let mut stripped = Vec::new();
				let mut locs = Vec::new();
				for (i, s) in stmts.iter().enumerate() {
					split.push(json!([
						s.get("stmt_location").and_then(|v| v.as_i64()).unwrap_or(0),
						s.get("stmt_len").and_then(|v| v.as_i64()).unwrap_or(0)
					]));
					let mut stmt = s.get("stmt").cloned().unwrap_or(Value::Null);
					let mut p = format!("/{i}");
					collect_locs(&stmt, &mut p, &mut locs);
					node_types(&stmt, &mut nodes);
					strip(&mut stmt, &["location"]);
					stripped.push(stmt);
				}
				let canon = serde_json::to_string(&Value::Array(stripped)).unwrap();
				t_canon += t.elapsed();
				sizes.push(canon.len());
				total_locs += locs.len();
				neg_locs += locs.iter().filter(|(_, l)| *l < 0).count();
				n_accept += 1;
				*labels.entry(label.clone()).or_default().entry("pg17_accept".into()).or_default() += 1;
				if label == "syntax_error" {
					writeln!(w_mismatch, "{}", json!({"id": id, "file": file, "kind": "label_syntax_error_pg17_accept"}))
						.unwrap();
				}
				writeln!(w_trees, "{{\"id\":\"{id}\",\"t\":{canon}}}").unwrap();
				let offs: Vec<i64> = locs.iter().map(|(_, l)| *l).collect();
				writeln!(w_locs, "{}", json!({"id": id, "l": offs, "s": split})).unwrap();
				let paths: Vec<Value> = locs.iter().map(|(p, l)| json!([p, l])).collect();
				writeln!(w_locpaths, "{}", json!({"id": id, "l": paths})).unwrap();

				// prost/serde path, for comparison
				let t = Instant::now();
				match pg_query::parse(&sql) {
					Ok(pr) => match serde_json::to_value(&pr.protobuf.stmts) {
						Ok(mut v) => {
							let nz = count_key(&v, "location", true);
							if nz != locs.iter().filter(|(_, l)| *l != 0).count() {
								n_loc_inconsistent += 1;
							}
							strip(&mut v, &["location", "stmt_location", "stmt_len"]);
							let s = serde_json::to_string(&v).unwrap();
							writeln!(w_prost, "{{\"id\":\"{id}\",\"t\":{s}}}").unwrap();
						}
						Err(_) => n_ser_fail += 1,
					},
					Err(_) => n_loc_inconsistent += 1,
				}
				t_prost += t.elapsed();
			}
		}
	}
	let total = t0.elapsed();
	sizes.sort_unstable();
	let pct = |p: f64| sizes.get(((sizes.len() as f64 - 1.0) * p) as usize).copied().unwrap_or(0);
	let summary = json!({
		"records": n_records,
		"unique_texts": n_unique,
		"duplicates_skipped": n_dup,
		"id_collisions_64bit": n_id_collision,
		"nul_unrepresentable": n_nul,
		"pg17_accept": n_accept,
		"pg17_reject": n_reject,
		"serialise_fail": n_ser_fail,
		"native_vs_prost_location_count_mismatch": n_loc_inconsistent,
		"labels_vs_pg17": labels,
		"total_locations": total_locs,
		"negative_locations": neg_locs,
		"tree_bytes": {"min": sizes.first(), "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": sizes.last(), "sum": sizes.iter().sum::<usize>()},
		"distinct_node_types": nodes.len(),
		"time_s": {"native_parse": t_native.as_secs_f64(), "canonicalise": t_canon.as_secs_f64(), "prost_parse_and_serde": t_prost.as_secs_f64(), "total": total.as_secs_f64()},
		"pg_version": "pg_query 6.2.1 (PG 17.7)",
	});
	std::fs::write(out_dir.join("summary.json"), serde_json::to_string_pretty(&summary).unwrap()).unwrap();
	let nodes_v: Value = serde_json::to_value(&nodes).unwrap();
	std::fs::write(out_dir.join("node_types.json"), serde_json::to_string_pretty(&nodes_v).unwrap()).unwrap();
	println!("{}", serde_json::to_string_pretty(&summary).unwrap());
}
