// Schema and fingerprint contract from herdrdev/herdr d6b40d4 (Apache-2.0).
// Adapted to a standalone synthetic-data oracle; no Herdr runtime is linked.
// See ../README.md and ../LICENSE.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

#[derive(Serialize, Deserialize)]
struct Snapshot {
    #[serde(default)] version: u32,
    workspaces: Vec<Workspace>, active: Option<usize>, selected: usize,
    #[serde(default)] sidebar_width: Option<u16>,
    #[serde(default)] sidebar_section_split: Option<f32>,
    #[serde(default)] collapsed_space_keys: HashSet<String>,
}
#[derive(Serialize, Deserialize)]
struct Workspace {
    #[serde(default)] id: Option<String>,
    #[serde(default)] custom_name: Option<String>,
    identity_cwd: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")] worktree_space: Option<Space>,
    #[serde(default)] public_pane_numbers: HashMap<u32, usize>,
    #[serde(default)] next_public_pane_number: usize,
    #[serde(default)] public_tab_numbers: Vec<usize>,
    #[serde(default)] next_public_tab_number: usize,
    tabs: Vec<Tab>,
    #[serde(default)] active_tab: usize,
}
#[derive(Serialize, Deserialize)]
struct Space { key: String, label: String, repo_root: PathBuf, checkout_path: PathBuf, is_linked_worktree: bool }
#[derive(Serialize, Deserialize)]
struct Tab {
    #[serde(default)] custom_name: Option<String>,
    layout: Layout, panes: HashMap<u32, Pane>, zoomed: bool,
    #[serde(default)] focused: Option<u32>,
    #[serde(default)] root_pane: Option<u32>,
}
#[derive(Serialize, Deserialize)]
struct Pane {
    cwd: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")] label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")] agent_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")] managed_agent_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")] agent_session: Option<Session>,
    #[serde(default, skip_serializing_if = "Option::is_none")] agent_resume: Option<Resume>,
    #[serde(default, skip_serializing_if = "Option::is_none")] launch_argv: Option<Vec<String>>,
}
#[derive(Serialize, Deserialize)]
struct Session { source: String, agent: String, kind: SessionKind, value: String }
#[derive(Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum SessionKind { Id, Path }
#[derive(Serialize, Deserialize)]
struct Resume { source: String, agent: String, argv: Vec<String> }
#[derive(Serialize, Deserialize)]
enum Direction { Horizontal, Vertical }
#[derive(Serialize, Deserialize)]
enum Layout { Pane(u32), Split { direction: Direction, ratio: f32, first: Box<Layout>, second: Box<Layout> } }

fn main() {
    let path = std::env::args().nth(1).expect("synthetic input file");
    let snapshots: Vec<Snapshot> = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    for snapshot in snapshots {
        let mut value = serde_json::to_value(&snapshot).unwrap();
        let mut collapsed: Vec<_> = snapshot.collapsed_space_keys.iter().collect();
        collapsed.sort_unstable();
        value["collapsed_space_keys"] = serde_json::to_value(collapsed).unwrap();
        let bytes = serde_json::to_vec(&value).unwrap();
        println!("{}", serde_json::json!({"sha256": format!("{:x}", Sha256::digest(&bytes)),
            "normalized": value}));
    }
}
