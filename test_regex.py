import re

texts = [
    "hãy đăng bài buôn mê lên tất cả các pages facebook",
    "đăng bài buôn mê lên facebook page dev test và site wordpress timchuyenbay",
    "đăng bài buôn mê lên tất cả page facebook và tất cả site wordpress",
    "đăng lên fb tất cả và wp tất cả",
    "đăng bài lên fb và wp site 1"
]

def _detect_preselection(text: str):
    pages_sel = ""
    wp_sel = ""
    fb_full = ""
    wp_full = ""

    # Split the text by "và", "hoặc", "hay" to isolate clauses
    clauses = re.split(r'\b(?:và|hoặc|hay)\b', text)
    
    for clause in clauses:
        fb_m = re.search(r"\b(tất cả(?: các)?\s+)?(?:page(?:s)?\s+)?(?:facebook|fb)\s*(?:page(?:s)?)?(?:\s+(.+?))?$", clause, re.IGNORECASE)
        if fb_m:
            prefix = (fb_m.group(1) or "").strip()
            suffix = (fb_m.group(2) or "").strip()
            if "tất cả" in prefix.lower() or "tất cả" in suffix.lower():
                pages_sel = "tất cả"
            else:
                pages_sel = suffix
            # clean trailing words like 'lên', 'trên'
            pages_sel = re.sub(r'\b(?:lên|trên)$', '', pages_sel).strip()
            fb_full = fb_m.group(0).strip()
            
        wp_m = re.search(r"\b(tất cả(?: các)?\s+)?(?:site(?:s)?\s+)?(?:wordpress|wp)\s*(?:site(?:s)?)?(?:\s+(.+?))?$", clause, re.IGNORECASE)
        if wp_m:
            prefix = (wp_m.group(1) or "").strip()
            suffix = (wp_m.group(2) or "").strip()
            if "tất cả" in prefix.lower() or "tất cả" in suffix.lower():
                wp_sel = "tất cả"
            else:
                wp_sel = suffix
            wp_sel = re.sub(r'\b(?:lên|trên)$', '', wp_sel).strip()
            wp_full = wp_m.group(0).strip()

    print(f"[{text}] -> FB='{pages_sel}' ({fb_full}) | WP='{wp_sel}' ({wp_full})")

for t in texts:
    _detect_preselection(t)

