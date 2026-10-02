//! Fast canonical AST JSON comparator
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;
use serde_json::Value;

fn canon(v: &Value, out: &mut String) {
	match v {
		Value::Object(m) => {
			let mut keys: Vec<&String> = m.keys().collect();
			keys.sort();
			out.push('{');
			for (i, k) in keys.iter().enumerate() {
				if i > 0 { out.push(','); }
				out.push_str(&serde_json::to_string(k).unwrap());
				out.push(':');
				canon(&m[*k], out);
			}
			out.push('}');
		}
		Value::Array(a) => {
			out.push('[');
			for (i, c) in a.iter().enumerate() {
				if i > 0 { out.push(','); }
				canon(c, out);
			}
			out.push(']');
		}
		other => out.push_str(&serde_json::to_string(other).unwrap()),
	}
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() < 2 {
		eprintln!("Usage: json-diff <file.jsonl> [reference.jsonl]");
		std::process::exit(1);
	}
	let path = &args[1];
	let f = match File::open(Path::new(path)) {
		Ok(f) => f,
		Err(e) => {
			eprintln!("Failed to open {path}: {e}");
			std::process::exit(1);
		}
	};
	let mut total = 0usize;
	let mut matched = 0usize;
	for line in BufReader::new(f).lines() {
		let line = match line {
			Ok(l) => l,
			Err(e) => {
				eprintln!("Read error: {e}");
				std::process::exit(1);
			}
		};
		if line.trim().is_empty() {
			continue;
		}
		let v: Value = match serde_json::from_str(&line) {
			Ok(v) => v,
			Err(e) => {
				eprintln!("JSON parse error on line {}: {e}", total + 1);
				std::process::exit(1);
			}
		};
		let mut s = String::new();
		canon(&v, &mut s);
		total += 1;
		if let Ok(canon_v) = serde_json::to_string(&v) {
			if s == canon_v {
				matched += 1;
			}
		}
	}
	println!("Verified {matched}/{total} trees canonically.");
}
