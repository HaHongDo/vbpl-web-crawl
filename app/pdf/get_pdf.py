import os
import requests


def get_vbpl_pdf(pdf_url, folder_path):
    os.makedirs(folder_path, exist_ok=True)

    file_name = os.path.join(folder_path, os.path.basename(pdf_url))

    if os.path.isfile(file_name):
        base_name, file_extension = os.path.splitext(file_name)
        index = 1
        while True:
            new_file_name = f"{base_name} ({index}){file_extension}"
            if not os.path.isfile(new_file_name):
                file_name = new_file_name
                break
            index += 1

    response = requests.get(pdf_url)

    if response.status_code == 200:
        with open(file_name, 'wb') as pdf_file:
            pdf_file.write(response.content)
    else:
        print(f"Failed to download PDF")

    response.close()
