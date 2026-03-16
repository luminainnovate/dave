import hashlib
import os
import re
import subprocess
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("Bob-Mover")

def sanitize_path(path: str) -> str:
    """
    Strictly sanitizes a path to prevent directory traversal and ensure safety.
    Removes leading slashes, '..', and drive letters.
    """
    # Remove drive letters (e.g., C:)
    path = re.sub(r'^[a-zA-Z]:', '', path)
    # Replace backslashes with forward slashes for consistency
    path = path.replace('\\', '/')
    # Remove leading slashes
    path = path.lstrip('/')
    # Normpath to resolve any internal . or ..
    # Then split and filter to ensure no component is '..'
    parts = os.path.normpath(path).split(os.sep)
    safe_parts = [p for p in parts if p and p != '..' and p != '.']
    
    return os.path.join(*safe_parts) if safe_parts else ""

def parse_tree_structure(text: str) -> List[str]:
    """
    Parses a directory tree snippet (├──, └──) into a list of full relative paths.
    """
    lines = text.splitlines()
    paths = []
    stack = []
    
    # Identify tree lines: lines containing one of the branch symbols
    branch_symbols = ["├──", "└──", "├─", "└─", "|──", "|---"]
    
    # Find the index of the first branch line
    first_branch_idx = -1
    for i, line in enumerate(lines):
        if any(sym in line for sym in branch_symbols):
            first_branch_idx = i
            break
            
    # Heuristic: the line preceding the first branch is often the root folder
    if first_branch_idx > 0:
        potential_root = lines[first_branch_idx - 1].strip()
        # Clean potential root name
        potential_root = potential_root.split('#')[0].strip().strip('/')
        if potential_root and not any(sym in potential_root for sym in branch_symbols + ["│", "|"]):
            stack = [potential_root]
            paths.append(potential_root)

    for i, line in enumerate(lines):
        if not line.strip():
            continue
            
        found_symbol = None
        symbol_idx = -1
        for sym in branch_symbols:
            idx = line.find(sym)
            if idx != -1:
                if symbol_idx == -1 or idx < symbol_idx:
                    symbol_idx = idx
                    found_symbol = sym
        
        if found_symbol:
            # If we don't have a root yet, this branch is at depth 0
            # Otherwise, symbol_idx 0 is depth 1 relative to the root
            depth = (symbol_idx // 4) + 1 if stack else symbol_idx // 4
            name = line[symbol_idx + len(found_symbol):].strip()
            name = name.split('#')[0].strip().strip('/')
            
            if name:
                stack = stack[:depth]
                stack.append(name)
                full_path = "/".join(stack)
                paths.append(full_path)
        else:
            # Check for root folder line explicitly (if not already handled by lookback)
            if i < first_branch_idx or first_branch_idx == -1:
                stripped = line.strip()
                if "/" in stripped and not any(c in stripped for c in ["├", "─", "└", "│", "|"]):
                    name = stripped.strip('/')
                    if name:
                        stack = [name]
                        paths.append(name)
    
    return list(set(paths))

def extract_path_from_content(content: str) -> Optional[str]:
    """
    Scans the first few lines of content for path indicators in comments.
    """
    lines = content.splitlines()[:5]
    path_indicator_re = re.compile(
        r"(?://|#|/\*|<!--)\s*(?P<path>[a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9]+)\s*(?:\*/|-->)?",
        re.IGNORECASE
    )
    
    for line in lines:
        match = path_indicator_re.search(line)
        if match:
            return match.group("path").replace('\\', '/')
    return None

def extract_snippets(messages: list) -> tuple[Dict[str, str], Optional[str]]:
    """
    Scans ALL messages for code blocks, parses trees, and maps files to paths.
    Returns (snippets_dict, suggested_root_name).
    """
    snippets = {}
    known_paths = []
    suggested_root = None
    
    # 1. First pass: Parse all directory trees from EVERY message role
    for msg in messages:
        content = msg.get("content", "")
        if any(c in content for c in ["├──", "└──", "├─", "└─"]):
            tree_paths = parse_tree_structure(content)
            known_paths.extend(tree_paths)
            if tree_paths:
                # Root is the shortest path in the tree that has 0 or 1 segments
                roots = [p for p in tree_paths if '/' not in p]
                if roots:
                    suggested_root = roots[0]
                elif not suggested_root:
                    suggested_root = tree_paths[0].split('/')[0]

    # 2. Second pass: Extract code blocks
    code_block_re = re.compile(r"```(?P<lang>[\w+#\-]+)?(?P<header>[^\n]*)\n(?P<content>.*?)```", re.DOTALL)
    filename_re = re.compile(r"(?P<filename>[a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]{1,5})", re.IGNORECASE)
    
    # Shell languages and command patterns to filter out
    SHELL_LANGS = {"bash", "sh", "shell", "zsh", "powershell", "ps1", "cmd", "batch"}
    COMMAND_PATTERNS = [
        r"^(?:\$|>|#)\s+",  # Prompt patterns
        r"^(?:cargo|npm|pnpm|yarn|python|pip|git|docker|kubectl|apt|brew|make)\s+", # Common CLIs
    ]
    command_re = re.compile("|".join(COMMAND_PATTERNS), re.MULTILINE | re.IGNORECASE)

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
            
        msg_content = msg.get("content", "")
        last_match_end = 0
        
        for match in code_block_re.finditer(msg_content):
            snippet_content = match.group("content").strip()
            header = (match.group("header") or "").strip()
            lang = (match.group("lang") or "").lower()
            
            # --- Shell Filtering ---
            if lang in SHELL_LANGS:
                last_match_end = match.end()
                continue
                
            # If no lang, check for command patterns in first 3 lines
            if not lang or lang == "txt":
                first_lines = "\n".join(snippet_content.splitlines()[:3])
                if command_re.search(first_lines):
                    last_match_end = match.end()
                    continue
            # ----------------------

            path = None
            
            # A. Check content comments
            path = extract_path_from_content(snippet_content)
            
            # B. Check header line
            if not path and header:
                potential_path = header.split()[0]
                if "." in potential_path:
                    path = potential_path
            
            # C. Check preceding text if still no path
            if not path:
                preceding_text = msg_content[last_match_end:match.start()].strip()
                if preceding_text:
                    search_area = preceding_text[-150:]
                    all_matches = filename_re.findall(search_area)
                    if all_matches:
                        filename = all_matches[-1]
                        path = filename

            # D. Tree Mapping: Try to resolve the path using the known tree
            if path:
                best_match = None
                # Sort by depth and length to find the most specific match
                sorted_kp = sorted(list(set(known_paths)), key=lambda x: (x.count('/'), len(x)), reverse=True)
                
                # Try 1: Exact match or specific file match
                for kp in sorted_kp:
                    if kp == path or kp.endswith("/" + path):
                        best_match = kp
                        break
                
                # Try 2: Smart Merge (match directory part)
                if not best_match and "/" in path:
                    dir_part = os.path.dirname(path)
                    filename = os.path.basename(path)
                    for kp in sorted_kp:
                        if kp == dir_part or kp.endswith("/" + dir_part):
                            best_match = "/".join([kp, filename])
                            break
                            
                if best_match:
                    path = best_match

            # E. Fallback
            if not path:
                ext = lang if lang and len(lang) < 5 else "txt"
                path = f"snippet_{len(snippets) + 1}.{ext}"
            
            safe_path = sanitize_path(path)
            if safe_path:
                snippets[safe_path] = snippet_content
                
            last_match_end = match.end()
            
    return snippets, suggested_root

def get_conversation_id(messages: list) -> str:
    """
    Generates a unique but stable ID for a conversation based on its first user message.
    """
    if not messages:
        return "empty"
    
    first_user_msg = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    hash_obj = hashlib.md5(first_user_msg.encode('utf-8'))
    return hash_obj.hexdigest()[:12]

def handle_move(messages: list) -> str:
    """
    Extracts snippets, parses trees, saves to stable folder, and opens in VS Code.
    """
    try:
        snippets, suggested_root = extract_snippets(messages)
        if not snippets:
            return "No code snippets found in the conversation context."

        conv_id = get_conversation_id(messages)
        prefix = suggested_root or "chat"
        
        # If no suggested root from tree, try to extract from first user message
        if not suggested_root:
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content", "").strip()
                    if content:
                        simplified = re.sub(r'[^a-zA-Z0-9]', '_', content)[:20].strip('_')
                        if simplified:
                            prefix = simplified
                        break
        
        target_dir = os.path.join("conversations", f"{prefix}_{conv_id}")
        os.makedirs(target_dir, exist_ok=True)
        
        saved_count = 0
        skipped_count = 0
        
        for rel_path, content in snippets.items():
            abs_path = os.path.join(target_dir, rel_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            
            if os.path.exists(abs_path):
                with open(abs_path, "r", encoding="utf-8") as f:
                    if f.read() == content:
                        skipped_count += 1
                        continue
            
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            saved_count += 1
            
        try:
            subprocess.run(["code", "."], cwd=target_dir, check=True, capture_output=True)
            vscode_msg = "Opened folder in VS Code."
        except Exception as e:
            logger.error(f"Failed to open VS Code: {e}")
            vscode_msg = f"Failed to open VS Code automatically (Error: {e})."

        return f"Moved {saved_count} files (skipped {skipped_count}) to `{target_dir}`\n\n{vscode_msg}"

    except Exception as e:
        logger.error(f"Error in handle_move: {e}")
        return f"Error while moving project: {e}"
