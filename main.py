import os

from bs4 import BeautifulSoup
from app.pdf.get_pdf import get_vbpl_pdf

# Opening the html file
HTMLFile = open("misc/vbpl.html", "r", encoding="utf8")

# Reading the file
index = HTMLFile.read()

# Creating a BeautifulSoup object and specifying the parser
S = BeautifulSoup(index, 'lxml')

# Using the select-one method to find the second element from the li tag
Tag = S.select_one('li:nth-of-type(2)')

# Using the decompose method
Tag.decompose()

# Using the prettify method to modify the code
# print(S.body.prettify())

# if __name__ == '__main__':
#     urls = ["https://bientap.vbpl.vn//FileData/TW/Lists/vbpq/Attachments/32801/VanBanGoc_Hien%20phap%202013.pdf",
#             "https://bientap.vbpl.vn//FileData/TW/Lists/vbpq/Attachments/139264/VanBanGoc_BO%20LUAT%2045%20QH14.pdf"]
#     store_folder = 'vbpl_pdf'
#     os.makedirs(store_folder, exist_ok=True)
#     for pdf_url in urls:
#         get_vbpl_pdf(pdf_url, store_folder)

