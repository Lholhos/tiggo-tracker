from bs4 import BeautifulSoup
import json

with open("wbc_dump.html", "r", encoding="utf-8") as f:
    html = f.read()

soup = BeautifulSoup(html, "html.parser")

out = []
for card in soup.select('.grid-card'):
    data = {}
    
    # Let's try to extract everything inside
    title_el = card.select_one('.heading-small')
    if title_el: data['title'] = title_el.get_text(strip=True)
    
    price_el = card.select_one('.price-large')
    if price_el: data['price'] = price_el.get_text(strip=True)
    
    for row in card.select('.wbc-list-item'):
        text = row.get_text(" ", strip=True)
        # Year, Mileage, etc usually in these list items
        if 'Year' in text or 'Mileage' in text or 'Location' in text:
            data['info'] = data.get('info', []) + [text]
            
    out.append(data)
    
print(json.dumps(out[:3], indent=2))
