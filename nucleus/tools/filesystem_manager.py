import os
import json
from pathlib import Path
import shutil
from typing import Optional, List, Dict, Any

def handle_filesystem(action: str, path: str, content: Optional[str] = None, target_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Universal file system handler for read, write, edit, delete, list operations.
    
    Args:
        action: 'read', 'write', 'append', 'delete', 'list', 'mkdir', 'copy', 'move'
        path: file or directory path
        content: content to write/append (for write/append actions)
        target_path: destination path (for copy/move actions)
    """
    try:
        path = os.path.expanduser(path)
        result = {"success": False, "action": action, "path": path}
        
        if action == "read":
            if not os.path.exists(path):
                result["error"] = f"File not found: {path}"
            elif os.path.isdir(path):
                result["error"] = f"Path is a directory, not a file: {path}"
            else:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    result["content"] = f.read()
                result["success"] = True
                
        elif action == "write":
            if content is None:
                result["error"] = "content parameter required for write action"
            else:
                os.makedirs(os.path.dirname(os.path.expanduser(path)), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                result["success"] = True
                result["message"] = f"Wrote {len(content)} bytes to {path}"
                
        elif action == "append":
            if content is None:
                result["error"] = "content parameter required for append action"
            else:
                os.makedirs(os.path.dirname(os.path.expanduser(path)), exist_ok=True)
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(content)
                result["success"] = True
                result["message"] = f"Appended {len(content)} bytes to {path}"
                
        elif action == "delete":
            if not os.path.exists(path):
                result["error"] = f"Path not found: {path}"
            elif os.path.isdir(path):
                shutil.rmtree(path)
                result["success"] = True
                result["message"] = f"Deleted directory: {path}"
            else:
                os.remove(path)
                result["success"] = True
                result["message"] = f"Deleted file: {path}"
                
        elif action == "list":
            if not os.path.exists(path):
                result["error"] = f"Path not found: {path}"
            elif not os.path.isdir(path):
                result["error"] = f"Path is not a directory: {path}"
            else:
                items = []
                for item in os.listdir(path):
                    item_path = os.path.join(path, item)
                    items.append({
                        "name": item,
                        "type": "directory" if os.path.isdir(item_path) else "file",
                        "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None
                    })
                result["items"] = items
                result["success"] = True
                
        elif action == "mkdir":
            os.makedirs(path, exist_ok=True)
            result["success"] = True
            result["message"] = f"Created directory: {path}"
            
        elif action == "copy":
            if target_path is None:
                result["error"] = "target_path parameter required for copy action"
            elif not os.path.exists(path):
                result["error"] = f"Source path not found: {path}"
            else:
                target_path = os.path.expanduser(target_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if os.path.isdir(path):
                    shutil.copytree(path, target_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(path, target_path)
                result["success"] = True
                result["message"] = f"Copied {path} to {target_path}"
                
        elif action == "move":
            if target_path is None:
                result["error"] = "target_path parameter required for move action"
            elif not os.path.exists(path):
                result["error"] = f"Source path not found: {path}"
            else:
                target_path = os.path.expanduser(target_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.move(path, target_path)
                result["success"] = True
                result["message"] = f"Moved {path} to {target_path}"
        else:
            result["error"] = f"Unknown action: {action}. Valid actions: read, write, append, delete, list, mkdir, copy, move"
            
        return result
        
    except Exception as e:
        return {"success": False, "action": action, "path": path, "error": str(e)}

if __name__ == "__main__":
    import sys
    try:
        input_data = json.load(sys.stdin)
        output = handle_filesystem(**input_data)
        print(json.dumps(output))
    except Exception as e:
        print(json.dumps({"success": False, "error": f"Input error: {str(e)}"}))
