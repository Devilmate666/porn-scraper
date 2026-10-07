import re

with open(r'C:\Users\Administrator\Desktop\cloudflare\index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the broken escape function in suggestDropdown
old = "const esc = (t) => String(t == null ? '' : t).replace(/[&<>\\\"]/g, c => ({ '&': '&', '<': '<', '>': '>', '\\\"': '\\\"', '\\'': '\\''' }[c]));"
new = "const esc = (t) => String(t == null ? '' : t).replace(/[&<>\\\"]/g, c => ({ '&': '&', '<': '<', '>': '>', '\\\"': '\"', '\\'': ''' }[c]));"

content = content.replace(old, new)

with open(r'C:\Users\Administrator\Desktop\cloudflare\index.html', 'w', encoding='utf-8') as f:
    f.write(content)
print('Fixed')