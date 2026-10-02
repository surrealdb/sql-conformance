//! Ratchet enforcement tool: ensures conformance metrics never regress below baseline
use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
struct ConformanceSummary {
	accepted: usize,
	rejected: usize,
	total: usize,
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() < 3 {
		eprintln!("Usage: ratchet <baseline_summary.json> <candidate_summary.json>");
		std::process::exit(1);
	}
	let baseline_path = &args[1];
	let candidate_path = &args[2];

	let baseline: ConformanceSummary = serde_json::from_reader(BufReader::new(
		File::open(Path::new(baseline_path)).expect("failed to open baseline"),
	))
	.expect("failed to parse baseline json");

	let candidate: ConformanceSummary = serde_json::from_reader(BufReader::new(
		File::open(Path::new(candidate_path)).expect("failed to open candidate"),
	))
	.expect("failed to parse candidate json");

	println!(
		"Baseline:  accepted={}/{} ({:.2}%)",
		baseline.accepted,
		baseline.total,
		(baseline.accepted as f64 / baseline.total as f64) * 100.0
	);
	println!(
		"Candidate: accepted={}/{} ({:.2}%)",
		candidate.accepted,
		candidate.total,
		(candidate.accepted as f64 / candidate.total as f64) * 100.0
	);

	if candidate.accepted < baseline.accepted {
		eprintln!(
			"❌ Conformance regression! Candidate accepted {} statements, below baseline of {}",
			candidate.accepted, baseline.accepted
		);
		std::process::exit(1);
	}

	println!("✅ Ratchet check passed: no regressions against baseline.");
}
