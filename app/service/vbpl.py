import logging
import math
import re
import copy
from datetime import datetime
from http import HTTPStatus
from typing import Dict

import requests

from app.entity.vbpl import VbplFullTextField
from app.helper.custom_exception import CommonException
from app.helper.enum import VbplTab, VbplType
from app.model import VbplToanVan, Vbpl, VbplRelatedDocument
from setting import setting
from app.helper.utility import convert_dict_to_pascal, get_html_node_text
from app.helper.db import LocalSession
from bs4 import BeautifulSoup

_logger = logging.getLogger(__name__)
find_id_regex = '(?<=ItemID=)\\d+'


class VbplService:
    _api_base_url = setting.VBPl_BASE_URL
    _default_row_per_page = 10
    _find_big_part_regex = '>((Phần)|(Phần thứ))'
    _find_section_regex = '>((Điều)|(Điều thứ))'
    _find_chapter_regex = '>Chương'
    _find_part_regex = '>Mục'
    _find_part_regex_2 = '>Mu.c'
    _find_mini_part_regex = '>Tiểu mục'
    _empty_related_doc_msg = 'Nội dung đang cập nhật'

    @classmethod
    def get_headers(cls) -> Dict:
        return {'Content-Type': 'application/json'}

    @classmethod
    def call(cls, method: str, url_path: str, query_params=None, json_data=None, timeout=30):
        url = cls._api_base_url + url_path
        headers = cls.get_headers()
        try:
            resp: requests.Response = requests.request(method, url, params=query_params, json=json_data,
                                                       headers=headers, timeout=timeout)
            if resp.status_code != 200:
                _logger.warning(
                    "Calling VBPL URL: %s, request_param %s, request_payload %s, http_code: %s, response: %s" %
                    (url, str(query_params), str(json_data), str(resp.status_code), resp.text))
            return resp
        except Exception as e:
            _logger.warning(f"Calling VBPL URL: {url},"
                            f" request_params {str(query_params)}, request_body {str(json_data)},"
                            f" error {str(e)}")
            raise e

    @classmethod
    def get_total_doc(cls, vbpl_type: VbplType):
        try:
            query_params = convert_dict_to_pascal({
                'is_viet_namese': True,
                'row_per_page': cls._default_row_per_page,
                'page': 2
            })

            resp = cls.call(method='GET',
                            url_path=f'/VBQPPL_UserControls/Publishing_22/TimKiem/p_{vbpl_type.value}.aspx',
                            query_params=query_params)
        except Exception as e:
            _logger.exception(e)
            raise CommonException(500, 'Get total doc')
        if resp.status_code == HTTPStatus.OK:
            soup = BeautifulSoup(resp.text, 'lxml')
            message = soup.find('div', {'class': 'message'})
            return int(message.find('strong').string)

    @classmethod
    def crawl_vbpl_all(cls, vbpl_type: VbplType):
        total_doc = cls.get_total_doc(vbpl_type)
        total_pages = math.ceil(total_doc / cls._default_row_per_page)
        prev_id_set = set()

        for i in range(total_pages):
            query_params = convert_dict_to_pascal({
                'is_viet_namese': True,
                'row_per_page': cls._default_row_per_page,
                'page': i + 1
            })

            try:
                resp = cls.call(method='GET',
                                url_path=f'/VBQPPL_UserControls/Publishing_22/TimKiem/p_{vbpl_type.value}.aspx',
                                query_params=query_params)
            except Exception as e:
                _logger.exception(e)
                raise CommonException(500, 'Crawl all doc')
            if resp.status_code == HTTPStatus.OK:
                soup = BeautifulSoup(resp.text, 'lxml')
                titles = soup.find_all('p', {"class": "title"})
                sub_titles = soup.find_all('div', {'class': "des"})
                check_last_page = False
                id_set = set()

                for j in range(len(titles)):
                    title = titles[j]
                    sub_title = sub_titles[j]

                    link = title.find('a')
                    doc_id = int(re.findall(find_id_regex, link.get('href'))[0])
                    if doc_id in prev_id_set:
                        check_last_page = True
                        break
                    id_set.add(doc_id)

                    new_vbpl = Vbpl(
                        id=doc_id,
                        title=get_html_node_text(link),
                        sub_title=get_html_node_text(sub_title)
                    )
                    cls.crawl_vbpl_info(new_vbpl)
                    # cls.crawl_vbpl_toanvan(new_vbpl)
                    cls.crawl_vbpl_related_doc(new_vbpl)
                    # print(new_vbpl)

                if check_last_page:
                    break

                prev_id_set = id_set
            # break

    @classmethod
    def update_vbpl_toanvan(cls, line, fulltext_obj: VbplFullTextField):
        line_content = get_html_node_text(line)
        check = False

        if re.search(cls._find_big_part_regex, str(line)):
            current_big_part_number_search = re.search('(?<=Phần thứ ).+', line_content)
            fulltext_obj.current_big_part_number = line_content[current_big_part_number_search.span()[0]:]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_big_part_name = get_html_node_text(next_node)

            fulltext_obj.reset_part()
            check = True

        if re.search(cls._find_chapter_regex, str(line)):
            fulltext_obj.current_chapter_number = re.findall('(?<=Chương ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_chapter_name = get_html_node_text(next_node)

            fulltext_obj.reset_part()
            check = True

        if re.search(cls._find_part_regex, str(line)) or re.search(cls._find_part_regex_2, str(line)):
            if re.search(cls._find_part_regex, str(line)):
                fulltext_obj.current_part_number = re.findall('(?<=Mục ).+', line_content)[0]
            else:
                fulltext_obj.current_part_number = re.findall('(?<=Mu.c ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_part_name = get_html_node_text(next_node)
            check = True

        if re.search(cls._find_mini_part_regex, str(line)):
            fulltext_obj.current_mini_part_number = re.findall('(?<=Tiểu mục ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_mini_part_name = get_html_node_text(next_node)
            check = True

        return fulltext_obj, check

    @classmethod
    def crawl_vbpl_toanvan(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.FULL_TEXT.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }

        try:
            resp = cls.call(method='GET', url_path=aspx_url, query_params=query_params)
        except Exception as e:
            _logger.exception(e)
            raise CommonException(500, 'Crawl vbpl toan van')

        if resp.status_code == HTTPStatus.OK:
            vbpl.html = resp.text
            soup = BeautifulSoup(resp.text, 'lxml')
            fulltext = soup.find('div', {"class": "toanvancontent"})

            lines = fulltext.find_all('p')
            vbpl_fulltext_obj = VbplFullTextField()

            for line in lines:
                if re.search(cls._find_section_regex, str(line)):
                    break

                vbpl_fulltext_obj, check = cls.update_vbpl_toanvan(line, vbpl_fulltext_obj)
                if check:
                    continue

            for line in lines:
                if re.search(cls._find_section_regex, str(line)):

                    line_content = get_html_node_text(line)
                    section_number_search = re.search('\\b\\d+', line_content)
                    section_number = int(section_number_search.group())

                    section_name = line_content[section_number_search.span()[1]:]
                    section_name_search = re.search('\\b\\w', section_name)
                    section_name_refined = section_name[section_name_search.span()[0]:]

                    current_fulltext_config = copy.deepcopy(vbpl_fulltext_obj)
                    # print(vbpl_fulltext_obj)
                    # print("Điều", section_number)
                    # print("Tên điều", section_name_refined)
                    content = []

                    next_node = line
                    while True:
                        next_node = next_node.find_next_sibling('p')

                        if next_node is None:
                            break

                        vbpl_fulltext_obj, check = cls.update_vbpl_toanvan(next_node, vbpl_fulltext_obj)
                        if check:
                            next_node = next_node.find_next_sibling('p')
                            continue

                        if re.search(cls._find_section_regex, str(next_node)) or re.search('_{2,}', str(next_node)):
                            section_content = '\n'.join(content)
                            # print(section_content)
                            with LocalSession.begin() as session:
                                new_fulltext_section = VbplToanVan(
                                    vbpl_id=vbpl.id,
                                    section_number=section_number,
                                    section_name=section_name_refined,
                                    section_content=section_content,
                                    chapter_name=current_fulltext_config.current_chapter_name,
                                    chapter_number=current_fulltext_config.current_chapter_number,
                                    mini_part_name=current_fulltext_config.current_mini_part_name,
                                    mini_part_number=current_fulltext_config.current_mini_part_number,
                                    part_name=current_fulltext_config.current_part_name,
                                    part_number=current_fulltext_config.current_part_number,
                                    big_part_name=current_fulltext_config.current_big_part_name,
                                    big_part_number=current_fulltext_config.current_big_part_number
                                )
                                session.add(new_fulltext_section)
                            break

                        content.append(get_html_node_text(next_node))

    @classmethod
    def crawl_vbpl_info(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.ATTRIBUTE.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }

        try:
            resp = cls.call(method='GET', url_path=aspx_url, query_params=query_params)
        except Exception as e:
            _logger.exception(e)
            raise CommonException(500, 'Crawl vbpl thuoc tinh')
        if resp.status_code == HTTPStatus.OK:
            soup = BeautifulSoup(resp.text, 'lxml')

            properties = soup.find('div', {"class": "vbProperties"})
            info = soup.find('div', {'class': 'vbInfo'})
            files = soup.find('ul', {'class': 'fileAttack'})

            table_rows = properties.find_all('tr')

            state_regex = 'Hiệu lực:'
            expiration_date_regex = 'Ngày hết hiệu lực:'

            date_format = '%d/%m/%Y'

            regex_dict = {
                'serial_number': 'Số ký hiệu',
                'issuance_date': 'Ngày ban hành',
                'effective_date': 'Ngày có hiệu lực',
                'gazette_date': 'Ngày đăng công báo',
                'issuing_authority': 'Cơ quan ban hành',
                'applicable_authority': 'Thông tin áp dụng',
                'doc_type': 'Loại văn bản'
            }

            def check_table_cell(field, node, input_vbpl: Vbpl):
                if re.search(regex_dict[field], str(node)):
                    field_value_node = node.find_next_sibling('td')
                    if field_value_node:
                        if field == 'issuance_date' or field == 'effective_date' or field == 'gazette_date':
                            try:
                                field_value = datetime.strptime(get_html_node_text(field_value_node), date_format)
                            except ValueError:
                                field_value = None
                        else:
                            field_value = get_html_node_text(field_value_node)
                        setattr(input_vbpl, field, field_value)

            for row in table_rows:
                table_cells = row.find_all('td')

                for cell in table_cells:
                    for key in regex_dict.keys():
                        check_table_cell(key, cell, vbpl)

            info_rows = info.find_all('li')

            for row in info_rows:
                if re.search(state_regex, str(row)):
                    vbpl.state = get_html_node_text(row)[len(state_regex):].strip()
                elif re.search(expiration_date_regex, str(row)):
                    date_content = get_html_node_text(row)[len(expiration_date_regex):].strip()
                    vbpl.expiration_date = datetime.strptime(date_content, date_format)

            file_urls = []
            file_links = files.find_all('li')

            for link in file_links:
                link_node = link.find_all('a')[0]
                if re.search('.*.pdf', get_html_node_text(link_node)):
                    href = link_node['href']
                    file_url = href[len('javascript:downloadfile('):-2].split(',')[1][1:-1]
                    file_urls.append(setting.VBPL_PDF_BASE_URL + file_url)

            vbpl.org_pdf_link = ' '.join(file_urls)

    @classmethod
    def crawl_vbpl_related_doc(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.RELATED_DOC.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }
        try:
            resp = cls.call(method='GET', url_path=aspx_url, query_params=query_params)
        except Exception as e:
            _logger.exception(e)
            raise CommonException(500, 'Crawl vbpl van ban lien quan')
        if resp.status_code == HTTPStatus.OK:
            soup = BeautifulSoup(resp.text, 'lxml')

            related_doc_node = soup.find('div', {'class': 'vbLienQuan'})
            if related_doc_node is None or re.search(cls._empty_related_doc_msg, get_html_node_text(related_doc_node)):
                print("No related doc")
                return

            doc_type_node = related_doc_node.find_all('td', {'class': 'label'})

            for node in doc_type_node:
                doc_type = get_html_node_text(node)
                related_doc_list_node = node.find_next_sibling('td').find('ul', {'class': 'listVB'})

                related_doc_list = related_doc_list_node.find_all('p', {'class': 'title'})
                for doc in related_doc_list:
                    link = doc.find('a')
                    doc_id = int(re.findall(find_id_regex, link.get('href'))[0])
                    new_vbpl_related_doc = VbplRelatedDocument(
                        source_id=vbpl.id,
                        related_id=doc_id,
                        doc_type=doc_type
                    )
                    # print(new_vbpl_related_doc)
