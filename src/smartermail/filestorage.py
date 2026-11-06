"""Personal SmarterMail file-storage: folder-tree walk and file download.

The webmail exposes the drive as one nested folder tree (``filestorage/folders``)
with the files inline on each node, and downloads a single file by GUID
(``filestorage/{id}/download``). Both the ``manage_file_storage`` workspace tool
and ``add_attachment(storage_file_id=...)`` need exactly that, so the wire lives
here once instead of being copied into the attachment tools.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .client import SmarterMailClient, SmarterMailError


def walk_folders(
    nodes: List[Dict[str, Any]], acc: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Flatten the ``subFolders`` tree into one list, parents first."""
    acc = acc if acc is not None else []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        acc.append(node)
        walk_folders(node.get("subFolders") or [], acc)
    return acc


def files_in(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Files carried by one folder node (the API omits the key when empty)."""
    files = node.get("files") or []
    return files if isinstance(files, list) else []


def storage_root(client: SmarterMailClient) -> Dict[str, Any]:
    """Root folder node of the mailbox's personal drive."""
    data = client.request("GET", "/api/v1/filestorage/folders")
    return data.get("folder") or {}


def find_file(root: Dict[str, Any], file_id: Any) -> Optional[Dict[str, Any]]:
    """Metadata for one file GUID anywhere in the tree, plus its folder id."""
    for node in walk_folders([root] if root else []):
        for item in files_in(node):
            if str(item.get("id")) == str(file_id):
                found = dict(item)
                found.setdefault("folderId", node.get("id"))
                return found
    return None


def file_metadata(client: SmarterMailClient, file_id: Any) -> Optional[Dict[str, Any]]:
    """Metadata for one file GUID, or ``None`` when the drive has no such file."""
    return find_file(storage_root(client), file_id)


def download_file(
    client: SmarterMailClient, file_id: Any
) -> Tuple[bytes, Optional[str]]:
    """Bytes of one file-storage file plus its stored name (``None`` if unknown).

    The download endpoint answers with the raw body and no usable filename, so
    the name comes from the folder tree. A metadata lookup that fails must not
    lose the bytes we already have: the caller gets ``None`` and picks a name.
    """
    blob = client.download("GET", f"/api/v1/filestorage/{file_id}/download")
    try:
        meta = file_metadata(client, file_id) or {}
    except SmarterMailError:
        return blob, None
    name = meta.get("fileName") or meta.get("name") or None
    return blob, (str(name) if name else None)
