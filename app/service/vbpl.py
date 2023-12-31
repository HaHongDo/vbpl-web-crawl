import asyncio
import os
import re
import copy
from datetime import datetime
from http import HTTPStatus
from typing import Dict
import aiohttp
import yarl
import concurrent.futures
from app.entity.vbpl import VbplFullTextField
from app.helper.custom_exception import CommonException
from app.helper.enum import VbplTab, VbplType
from time import sleep
from app.helper.logger import setup_logger
from app.model import VbplToanVan, Vbpl, VbplRelatedDocument, VbplDocMap
from app.model.vbpl import VbplSubPart
from app.service.get_pdf import get_document
from setting import setting
from app.helper.utility import convert_dict_to_pascal, get_html_node_text, convert_datetime_to_str, \
    concetti_query_params_url_encode, convert_str_to_datetime, check_header_tag
from app.helper.db import LocalSession
from urllib.parse import quote
import Levenshtein
from bs4 import BeautifulSoup
import py7zr

_logger = setup_logger('vbpl_logger', 'log/vbpl.log')
find_id_regex = '(?<=ItemID=)\\d+'


class VbplService:
    _api_base_url = setting.VBPl_BASE_URL
    _default_row_per_page = 130
    _max_threads = 8
    _find_big_part_regex = '^((Phần)|(Phần thứ)) (nhất|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)$'
    _find_section_regex = '^((Điều)|(Điều thứ)) \\d+'
    _find_chapter_regex = '^Chương [IVX]+'
    _find_part_regex = '^Mục [IVX]+'
    _find_part_regex_2 = '^Mu.c [IVX]+'
    _find_mini_part_regex = '^Tiểu mục [IVX]+'
    _find_start_sub_part_regex = '^PHỤ LỤC$'
    _find_sub_part_regex = '^Phụ(\\s)*(\\n)*lục [IVX]+'
    _empty_related_doc_msg = 'Nội dung đang cập nhật'
    _concetti_base_url = setting.CONCETTI_BASE_URL
    _tvpl_base_url = setting.TVPL_BASE_URL
    _cong_bao_base_url = setting.CONG_BAO_BASE_URL
    _luat_vn_base_url = setting.LUAT_VN_BASE_URL

    @classmethod
    def get_headers(cls) -> Dict:
        return {'Content-Type': 'application/json'}

    # base url call to use in later functions
    @classmethod
    async def call(cls, method: str, url_path: str, query_params=None, json_data=None, timeout=90):
        url = cls._api_base_url + url_path
        headers = cls.get_headers()
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.request(method, url, params=query_params, json=json_data, timeout=timeout,
                                           headers=headers) as resp:
                    await resp.text()
            if resp.status != HTTPStatus.OK:
                _logger.warning(
                    "Calling VBPL URL: %s, request_param %s, request_payload %s, http_code: %s, response: %s" %
                    (url, str(query_params), str(json_data), str(resp.status), resp.text))
            return resp
        except Exception as e:
            _logger.warning(f"Calling VBPL URL: {url},"
                            f" request_params {str(query_params)}, request_body {str(json_data)},"
                            f" error {str(e)}")

    # get total number of vbpl
    @classmethod
    async def get_total_doc(cls, vbpl_type: VbplType):
        try:
            query_params = convert_dict_to_pascal({
                'row_per_page': cls._default_row_per_page,
                'page': 2,
            })

            resp = await cls.call(method='GET',
                                  url_path=f'/VBQPPL_UserControls/Publishing_22/TimKiem/p_{vbpl_type.value}.aspx?IsVietNamese=True',
                                  query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                message = soup.find('div', {'class': 'message'})
                return int(message.find('strong').string)
        except Exception as e:
            _logger.exception(f'Get total vbpl doc {e}')
            raise CommonException(500, 'Get total doc')

    @classmethod
    async def crawl_all_vbpl(cls, vbpl_type: VbplType):
        # total_doc = await cls.get_total_doc(vbpl_type)
        total_pages = 1000
        full_id_list = []

        # crawl all vbpl info and full text using multi thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=cls._max_threads) as executor:
            info_and_fulltext_coroutines = [cls.crawl_vbpl_in_one_page(page, full_id_list, vbpl_type) for page in
                                            range(1, total_pages + 1)]
            executor.map(asyncio.run, info_and_fulltext_coroutines)

        # crawl vbpl relate doc using multi thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=cls._max_threads) as executor:
            related_doc_coroutines = [cls.crawl_vbpl_related_doc(doc_id) for doc_id in full_id_list]
            executor.map(asyncio.run, related_doc_coroutines)

        # crawl vbpl doc map using multi thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=cls._max_threads) as executor:
            doc_map_coroutines = [cls.crawl_vbpl_doc_map(doc_id, vbpl_type) for doc_id in full_id_list]
            executor.map(asyncio.run, doc_map_coroutines)

    @classmethod
    async def crawl_vbpl_in_one_page(cls, page, full_id_list, vbpl_type: VbplType):
        query_params = convert_dict_to_pascal({
            'row_per_page': cls._default_row_per_page,
            'page': page
        })
        progress = 0
        max_progress = cls._default_row_per_page

        try:
            resp = await cls.call(method='GET',
                                  url_path=f'/VBQPPL_UserControls/Publishing_22/TimKiem/p_{vbpl_type.value}.aspx?IsVietNamese=True',
                                  query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                titles = soup.find_all('p', {"class": "title"})
                sub_titles = soup.find_all('div', {'class': "des"})
                id_set = set()

                for j in range(len(titles)):
                    title = titles[j]
                    sub_title = sub_titles[j]

                    link = title.find('a')
                    doc_id = int(re.findall(find_id_regex, link.get('href'))[0])

                    _logger.info(f"Crawling vbpl {doc_id}")
                    id_set.add(doc_id)
                    full_id_list.append(doc_id)

                    # check for existing vbpl
                    with LocalSession.begin() as session:
                        check_vbpl = session.query(Vbpl).filter(Vbpl.id == doc_id).first()

                    # if it does not exist, add to db
                    new_vbpl = Vbpl(
                        id=doc_id,
                        title=get_html_node_text(link),
                        sub_title=get_html_node_text(sub_title)
                    )
                    vbpl_fulltext = None
                    vbpl_sub_part = None

                    if vbpl_type == VbplType.PHAP_QUY:
                        await cls.crawl_vbpl_phapquy_info(new_vbpl)
                        await cls.crawl_vbpl_pdf(new_vbpl, vbpl_type)
                        vbpl_fulltext, vbpl_sub_part = await cls.crawl_vbpl_phapquy_fulltext(new_vbpl)
                        await cls.search_concetti(new_vbpl)
                        await cls.enrich_vbpl_sector(new_vbpl)

                    elif vbpl_type == VbplType.HOP_NHAT:
                        await cls.crawl_vbpl_hopnhat_info(new_vbpl)
                        await cls.crawl_vbpl_pdf(new_vbpl, vbpl_type)
                        await cls.crawl_vbpl_hopnhat_fulltext(new_vbpl)
                        await cls.search_concetti(new_vbpl)
                        await cls.enrich_vbpl_sector(new_vbpl)
                        vbpl_fulltext, vbpl_sub_part = await cls.additional_html_crawl(new_vbpl)

                    # add to db
                    await cls.push_vbpl_to_db(doc_id, new_vbpl, vbpl_fulltext, vbpl_sub_part)

                    # update progress
                    progress += 1
                    _logger.info(f'Finished crawling vbpl {doc_id}')
                    _logger.info(f"Page {page} progress: {progress}/{max_progress}")
            sleep(3)
        except Exception as e:
            _logger.exception(f'Crawl all doc in page {page} {e}')
            raise CommonException(500, 'Crawl all doc')

    @classmethod
    async def push_vbpl_to_db(cls, doc_id, new_vbpl, vbpl_fulltext, vbpl_sub_part):
        with LocalSession.begin() as session:
            check_vbpl = session.query(Vbpl).filter(Vbpl.id == doc_id).first()
            if check_vbpl is not None:
                # upsert vbpl
                update_vbpl = {
                    'file_link': new_vbpl.file_link,
                    'title': new_vbpl.title,
                    'doc_type': new_vbpl.doc_type,
                    'serial_number': new_vbpl.serial_number,
                    'issuance_date': new_vbpl.issuance_date,
                    'effective_date': new_vbpl.effective_date,
                    'expiration_date': new_vbpl.expiration_date,
                    'gazette_date': new_vbpl.gazette_date,
                    'state': new_vbpl.state,
                    'issuing_authority': new_vbpl.issuing_authority,
                    'applicable_information': new_vbpl.applicable_information,
                    'html': new_vbpl.html,
                    'org_pdf_link': new_vbpl.org_pdf_link,
                    'sub_title': new_vbpl.sub_title,
                    'sector': new_vbpl.sector,
                }
                session.query(Vbpl).filter(Vbpl.id == doc_id).update(update_vbpl)
            else:
                session.add(new_vbpl)

            if vbpl_fulltext is not None:
                for fulltext_section in vbpl_fulltext:
                    check_fulltext = session.query(VbplToanVan).filter(
                        VbplToanVan.vbpl_id == fulltext_section.vbpl_id,
                        VbplToanVan.section_number == fulltext_section.section_number).first()
                    if check_fulltext is None:
                        session.add(fulltext_section)
                    else:
                        # upsert toan van
                        updated_toan_van = {
                            'section_name': fulltext_section.section_name,
                            'section_content': fulltext_section.section_content,
                            'chapter_number': fulltext_section.chapter_number,
                            'chapter_name': fulltext_section.chapter_name,
                            'part_number': fulltext_section.part_number,
                            'part_name': fulltext_section.part_name,
                            'mini_part_number': fulltext_section.mini_part_number,
                            'mini_part_name': fulltext_section.mini_part_name,
                            'big_part_number': fulltext_section.big_part_number,
                            'big_part_name': fulltext_section.big_part_name,
                        }
                        session.query(VbplToanVan).filter(
                            VbplToanVan.vbpl_id == fulltext_section.vbpl_id,
                            VbplToanVan.section_number == fulltext_section.section_number).update(updated_toan_van)
            if vbpl_sub_part is not None:
                for sub_part in vbpl_sub_part:
                    check_sub_part = session.query(VbplSubPart).filter(
                        VbplSubPart.vbpl_id == sub_part.vbpl_id,
                        VbplSubPart.sub_section_part_number == sub_part.sub_section_part_number).first()
                    if check_sub_part is None:
                        session.add(sub_part)
                    else:
                        # upsert sub parts
                        updated_sub_part = {
                            'sub_section_title': sub_part.sub_section_title,
                            'sub_section_part_title': sub_part.sub_section_part_title
                        }
                        session.query(VbplSubPart).filter(
                            VbplSubPart.vbpl_id == sub_part.vbpl_id,
                            VbplSubPart.sub_section_part_number == sub_part.sub_section_part_number).update(updated_sub_part)

    @classmethod
    def update_vbpl_phapquy_fulltext(cls, line, fulltext_obj: VbplFullTextField):
        line_content = get_html_node_text(line)
        check = False

        if re.search(cls._find_big_part_regex, line_content):
            current_big_part_number_search = re.search('(?<=Phần thứ ).+', line_content)
            fulltext_obj.current_big_part_number = line_content[current_big_part_number_search.span()[0]:]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_big_part_name = get_html_node_text(next_node)

            fulltext_obj.reset_part()
            check = True

        if re.search(cls._find_chapter_regex, line_content):
            fulltext_obj.current_chapter_number = re.findall('(?<=Chương ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_chapter_name = get_html_node_text(next_node)

            fulltext_obj.reset_part()
            check = True

        if re.search(cls._find_part_regex, line_content) or re.search(cls._find_part_regex_2, line_content):
            if re.search(cls._find_part_regex, line_content):
                fulltext_obj.current_part_number = re.findall('(?<=Mục ).+', line_content)[0]
            else:
                fulltext_obj.current_part_number = re.findall('(?<=Mu.c ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_part_name = get_html_node_text(next_node)
            check = True

        if re.search(cls._find_mini_part_regex, line_content):
            fulltext_obj.current_mini_part_number = re.findall('(?<=Tiểu mục ).+', line_content)[0]
            next_node = line.find_next_sibling('p')
            fulltext_obj.current_mini_part_name = get_html_node_text(next_node)
            check = True

        return fulltext_obj, check

    @classmethod
    def process_html_full_text(cls, vbpl: Vbpl, lines):
        vbpl_fulltext_obj = VbplFullTextField()
        results = []

        # init vbpl fulltext object
        for line in lines:
            # if line.name not in ['p', 'div'] or not check_header_tag(line.name):
            #     continue

            line_content = get_html_node_text(line)
            if re.search(cls._find_section_regex, line_content):
                break

            vbpl_fulltext_obj, check = cls.update_vbpl_phapquy_fulltext(line, vbpl_fulltext_obj)
            if check:
                continue

        # process fulltext line by line
        for line_index, line in enumerate(lines):
            # if line.name not in ['p', 'div'] or not check_header_tag(line.name):
            #     continue

            line_content = get_html_node_text(line)

            if re.search(cls._find_start_sub_part_regex, line_content):
                new_vbpl_sub_part = cls.process_vbpl_sub_part(vbpl.id, lines[line_index:])
                return results, new_vbpl_sub_part

            if re.search(cls._find_section_regex, line_content):
                section_number_search = re.search('\\b\\d+', line_content)
                section_number = int(section_number_search.group())

                section_name = line_content[section_number_search.span()[1]:]
                section_name_refined = None
                section_name_search = re.search('\\b\\w', section_name)
                if section_name_search:
                    section_name_refined = section_name[section_name_search.span()[0]:]

                current_fulltext_config = copy.deepcopy(vbpl_fulltext_obj)
                content = []
                if section_name_refined is not None and len(section_name_refined) >= 400:
                    content.append(section_name_refined)
                    section_name_refined = None

                next_node = line
                while True:
                    next_node = next_node.find_next_sibling('p')

                    if next_node is None:
                        break

                    node_content = get_html_node_text(next_node)

                    vbpl_fulltext_obj, check = cls.update_vbpl_phapquy_fulltext(next_node, vbpl_fulltext_obj)
                    if check:
                        next_node = next_node.find_next_sibling('p')
                        if next_node is None:
                            break
                        continue

                    if (re.search(cls._find_section_regex, node_content)
                        or re.search('_{2,}', node_content)
                        or next_node.find_next_sibling('p') is None) \
                            or re.search(cls._find_start_sub_part_regex, node_content):
                        section_content = '\n'.join(content)

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
                        results.append(new_fulltext_section)
                        break

                    content.append(get_html_node_text(next_node))
        return results, None

    @classmethod
    def process_vbpl_sub_part(cls, vbpl_id, sub_part_lines):
        sub_section_title = get_html_node_text(sub_part_lines[1])
        vbpl_sub_parts = []

        regex_dict = {
            '^Phụ(\\s)*(\\n)*lục [IVX]+': '(?<=lục )[IVX]+',
            '^Phụ(\\s)*(\\n)*lục \\d+': '(?<=lục )\\d+',
            # '^\\d+\\.': '^\\d+(?=\\.)',
            # '^\\d+-': '^\\d+(?=-)'
        }
        is_sub_section_part_title = False

        for i in range(2, len(sub_part_lines)):
            if is_sub_section_part_title:
                is_sub_section_part_title = False
                continue

            line = sub_part_lines[i]
            # if line.name not in ['p', 'div'] or not check_header_tag(line.name):
            #     continue

            line_content = get_html_node_text(line)

            for check_regex in regex_dict.keys():
                if re.search(check_regex, line_content):
                    extract_regex = regex_dict[check_regex]
                    current_sub_part_reg = re.search(extract_regex, line_content)

                    # get sub part number
                    current_sub_part = line_content[current_sub_part_reg.span()[0]:current_sub_part_reg.span()[1]]
                    # skip if sub part number is not numeral or roman numeral
                    if not re.search('^[IVX]+$', current_sub_part) and not re.search('^\\d+$', current_sub_part):
                        continue

                    current_sub_part_title = line_content[current_sub_part_reg.span()[1]:].strip()
                    # if the sub part title is not right beside the sub part number, it is below it
                    if current_sub_part_title == '':
                        current_sub_part_title_node = sub_part_lines[i + 1]
                        current_sub_part_title = get_html_node_text(current_sub_part_title_node)
                        is_sub_section_part_title = True

                    new_vbpl_sub_part = VbplSubPart(
                        vbpl_id=vbpl_id,
                        sub_section_title=sub_section_title,
                        sub_section_part_number=current_sub_part,
                        sub_section_part_title=current_sub_part_title
                    )
                    vbpl_sub_parts.append(new_vbpl_sub_part)
                    break
        if len(vbpl_sub_parts) == 0:
            vbpl_sub_parts.append(VbplSubPart(
                vbpl_id=vbpl_id,
                sub_section_title=sub_section_title,
                sub_section_part_number='0',
                sub_section_part_title=None
            ))
        return vbpl_sub_parts

    @classmethod
    async def crawl_vbpl_phapquy_fulltext(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.FULL_TEXT.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }
        results = []
        vbpl_sub_parts = None

        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)

            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                fulltext = soup.find('div', {"class": "toanvancontent"})

                if fulltext is None:
                    return await cls.additional_html_crawl(vbpl)

                vbpl.html = str(fulltext)

                lines = fulltext.find_all('p')
                if len(lines) == 0:
                    lines = fulltext.find_all('div')
                if len(lines) == 0:
                    return await cls.additional_html_crawl(vbpl)
                results, vbpl_sub_parts = cls.process_html_full_text(vbpl, lines)
        except Exception as e:
            _logger.exception(f'Crawl vbpl phapquy fulltext {vbpl.id} {e}')
            raise CommonException(500, 'Crawl vbpl toan van')

        return results, vbpl_sub_parts

    # for vbpl hopnhat it does not have html like vbpl phapquy so we can only fetch its doc/pdf
    # of course we'll still try to find its html in tvpl, you can find it in crawl_vbpl_in_one_page
    @classmethod
    async def crawl_vbpl_hopnhat_fulltext(cls, vbpl: Vbpl):
        if vbpl.org_pdf_link is not None and vbpl.org_pdf_link.strip() != '':
            return

        aspx_url = f'/TW/Pages/vbpq-{VbplTab.FULL_TEXT_HOP_NHAT.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }

        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                vbpl_view = soup.find('div', {'class': 'vbProperties'})
                document_view_object = vbpl_view.find('object')
                if document_view_object is not None:
                    document_link = re.findall('.+.pdf', document_view_object.get('data'))[0]
                    vbpl.org_pdf_link = setting.VBPL_PDF_BASE_URL + document_link
                    vbpl.file_link = get_document(vbpl.org_pdf_link, True)
                else:
                    aspx_url = f'/TW/Pages/vbpq-{VbplTab.FULL_TEXT_HOP_NHAT_2.value}.aspx'
                    query_params = {
                        'ItemID': vbpl.id
                    }

                    resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)

                    if resp.status == HTTPStatus.OK:
                        soup = BeautifulSoup(await resp.text(), 'lxml')
                        vbpl_view = soup.find('div', {'class': 'vbProperties'})
                        pdf_view_object = vbpl_view.find('object')
                        if pdf_view_object is not None:
                            pdf_link = re.findall('.+.pdf', pdf_view_object.get('data'))[0]
                            vbpl.org_pdf_link = setting.VBPL_PDF_BASE_URL + pdf_link
                            vbpl.file_link = get_document(vbpl.org_pdf_link, True)
        except Exception as e:
            _logger.exception(f'Crawl vbpl hopnhat fulltext {vbpl.id} {e}')
            raise CommonException(500, 'Crawl vbpl hop nhat toan van')

    @classmethod
    async def crawl_vbpl_hopnhat_info(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.ATTRIBUTE_HOP_NHAT.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }

        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')

                properties = soup.find('div', {"class": "vbProperties"})
                if properties is None:
                    return

                table_rows = properties.find_all('tr')
                date_format = '%d/%m/%Y'

                bread_crumbs = soup.find('div', {"class": "box-map"})
                title = bread_crumbs.find('a', {"href": ""})
                if vbpl.title is None:
                    vbpl.title = title.text.strip()
                sub_title = soup.find('td', {'class': 'title'})
                if vbpl.sub_title is None:
                    vbpl.sub_title = sub_title.text.strip()

                # regex dict to automate info extraction
                regex_dict = {
                    'serial_number': 'Số ký hiệu',
                    'effective_date': 'Ngày xác thực',
                    'gazette_date': 'Ngày đăng công báo',
                    'issuing_authority': 'Cơ quan ban hành',
                    'doc_type': 'Loại VB được sửa đổi bổ sung'
                }

                def check_table_cell(field, node, input_vbpl: Vbpl):
                    if re.search(regex_dict[field], str(node)):
                        field_value_node = node.find_next_sibling('td')
                        if field_value_node:
                            if field == 'effective_date' or field == 'gazette_date':
                                try:
                                    field_value = datetime.strptime(get_html_node_text(field_value_node), date_format)
                                except ValueError:
                                    field_value = None
                            else:
                                field_value = get_html_node_text(field_value_node)
                            setattr(input_vbpl, field, field_value)

                # extract information based on regex dict
                for row in table_rows:
                    table_cells = row.find_all('td')

                    for cell in table_cells:
                        for key in regex_dict.keys():
                            check_table_cell(key, cell, vbpl)

        except Exception as e:
            _logger.exception(f'Crawl vbpl hopnhat info {vbpl.id} {e}')
            raise CommonException(500, 'Crawl vbpl thuoc tinh')

    # quite similar to crawl_vbpl_hopnhat_info but only a few changes because this web is retarded
    # I split into 2 functions to avoid confusions
    @classmethod
    async def crawl_vbpl_phapquy_info(cls, vbpl: Vbpl):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.ATTRIBUTE.value}.aspx'
        query_params = {
            'ItemID': vbpl.id
        }

        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')

                properties = soup.find('div', {"class": "vbProperties"})
                info = soup.find('div', {'class': 'vbInfo'})
                if properties is None:
                    return

                bread_crumbs = soup.find('div', {"class": "box-map"})

                title = bread_crumbs.find('a', {"href": ""})
                if vbpl.title is None:
                    vbpl.title = title.text.strip()
                sub_title = soup.find('td', {'class': 'title'})
                if vbpl.sub_title is None:
                    vbpl.sub_title = sub_title.text.strip()

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
                    'applicable_information': 'Thông tin áp dụng',
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

                if info is not None:
                    info_rows = info.find_all('li')

                    for row in info_rows:
                        if re.search(state_regex, str(row)):
                            vbpl.state = get_html_node_text(row)[len(state_regex):].strip()
                        elif re.search(expiration_date_regex, str(row)):
                            date_content = get_html_node_text(row)[len(expiration_date_regex):].strip()
                            vbpl.expiration_date = datetime.strptime(date_content, date_format)

        except Exception as e:
            _logger.exception(f'Crawl vbpl phapquy info {vbpl.id} {e}')
            raise CommonException(500, 'Crawl vbpl thuoc tinh')

    @classmethod
    async def crawl_vbpl_related_doc(cls, vbpl_id):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.RELATED_DOC.value}.aspx'
        query_params = {
            'ItemID': vbpl_id
        }
        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')

                related_doc_node = soup.find('div', {'class': 'vbLienQuan'})
                if related_doc_node is None or re.search(cls._empty_related_doc_msg,
                                                         get_html_node_text(related_doc_node)):
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
                            source_id=vbpl_id,
                            related_id=doc_id,
                            doc_type=doc_type
                        )
                        with LocalSession.begin() as session:
                            check_related_doc = session.query(VbplRelatedDocument).filter(
                                VbplRelatedDocument.source_id == new_vbpl_related_doc.source_id,
                                VbplRelatedDocument.related_id == new_vbpl_related_doc.related_id).first()
                            if check_related_doc is None:
                                session.add(new_vbpl_related_doc)
                            else:
                                # upsert vbpl_related_document
                                update_data = {
                                    'doc_type': doc_type
                                }
                                session.query(VbplRelatedDocument).filter(
                                    VbplRelatedDocument.source_id == new_vbpl_related_doc.source_id,
                                    VbplRelatedDocument.related_id == new_vbpl_related_doc.related_id).update(
                                    update_data)

            sleep(1)
        except Exception as e:
            _logger.exception(f'Crawl vbpl related doc {vbpl_id} {e}')
            raise CommonException(500, 'Crawl vbpl van ban lien quan')

    @classmethod
    async def crawl_vbpl_doc_map(cls, vbpl_id, vbpl_type: VbplType):
        aspx_url = f'/TW/Pages/vbpq-{VbplTab.DOC_MAP.value}.aspx'
        if vbpl_type == VbplType.HOP_NHAT:
            aspx_url = f'/TW/Pages/vbpq-{VbplTab.DOC_MAP_HOP_NHAT.value}.aspx'
        query_params = {
            'ItemID': vbpl_id
        }
        try:
            resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                if vbpl_type == VbplType.PHAP_QUY:
                    doc_map_title_nodes = soup.find_all('div', {'class': re.compile('title')})
                    for doc_map_title_node in doc_map_title_nodes:
                        doc_map_title = get_html_node_text(doc_map_title_node)

                        doc_map_content_node = doc_map_title_node.find_next_sibling('div')
                        doc_map_list = doc_map_content_node.find_all('li')
                        for doc_map in doc_map_list:
                            link = doc_map.find('a')
                            link_ref = re.findall(find_id_regex, link.get('href'))
                            doc_map_id = None

                            # in some cases, the doc map id is embedded in the link but some cases it is not
                            # so we have to manually search for those cases, like i said, this web is retarded
                            if len(link_ref) > 0:
                                doc_map_id = int(link_ref[0])
                            else:
                                doc_title = link.text.strip()

                                search_resp = await cls.call(method='GET',
                                                             url_path=f'/VBQPPL_UserControls/Publishing_22/TimKiem/p_{vbpl_type.value}.aspx?IsVietNamese=True',
                                                             query_params=convert_dict_to_pascal({
                                                                 'row_per_page': cls._default_row_per_page,
                                                                 'page': 1,
                                                                 'keyword': doc_title
                                                             }))
                                if search_resp.status == HTTPStatus.OK:
                                    search_soup = BeautifulSoup(await search_resp.text(), 'lxml')
                                    titles = search_soup.find_all('p', {"class": "title"})
                                    if len(titles) > 0:
                                        search_link = titles[0].find('a')
                                        doc_map_id = int(re.findall(find_id_regex, search_link.get('href'))[0])

                            new_vbpl_doc_map = VbplDocMap(
                                source_id=vbpl_id,
                                doc_map_id=doc_map_id,
                                doc_map_type=doc_map_title
                            )
                            with LocalSession.begin() as session:
                                check_doc_map = session.query(VbplDocMap).filter(
                                    VbplDocMap.source_id == new_vbpl_doc_map.source_id,
                                    VbplDocMap.doc_map_id == new_vbpl_doc_map.doc_map_id).first()
                                if check_doc_map is None:
                                    session.add(new_vbpl_doc_map)
                                else:
                                    # upsert doc_map for phap quy
                                    update_data = {
                                        'doc_map_type': doc_map_title
                                    }
                                    session.query(VbplDocMap).filter(
                                        VbplDocMap.source_id == new_vbpl_doc_map.source_id,
                                        VbplDocMap.doc_map_id == new_vbpl_doc_map.doc_map_id).update(update_data)

                elif vbpl_type == VbplType.HOP_NHAT:
                    doc_map_nodes = soup.find_all('div', {'class': 'w'})
                    if len(doc_map_nodes) > 1:
                        doc_map_nodes = doc_map_nodes[:-1]
                    else:
                        return
                    for doc_map_node in doc_map_nodes:
                        link = doc_map_node.find('a')
                        link_ref = re.findall(find_id_regex, link.get('href'))
                        doc_map_id = int(link_ref[0])

                        new_vbpl_doc_map = VbplDocMap(
                            source_id=vbpl_id,
                            doc_map_id=doc_map_id,
                            doc_map_type='Văn bản được hợp nhất'
                        )
                        with LocalSession.begin() as session:
                            check_doc_map = session.query(VbplDocMap).filter(
                                VbplDocMap.source_id == new_vbpl_doc_map.source_id,
                                VbplDocMap.doc_map_id == new_vbpl_doc_map.doc_map_id).first()
                            if check_doc_map is None:
                                session.add(new_vbpl_doc_map)
                            else:
                                # upsert doc_map for hop nhat
                                update_data = {
                                    'doc_map_type': 'Văn bản được hợp nhất'
                                }
                                session.query(VbplDocMap).filter(
                                    VbplDocMap.source_id == new_vbpl_doc_map.source_id,
                                    VbplDocMap.doc_map_id == new_vbpl_doc_map.doc_map_id).update(update_data)
            sleep(1)
        except Exception as e:
            _logger.exception(f'Crawl vbpl doc map {vbpl_id} {e}')
            raise CommonException(500, 'Crawl vbpl luoc do')

    # fetch additional data from concetti
    @classmethod
    async def search_concetti(cls, vbpl: Vbpl):
        search_url = f'/documents/search'
        key_type = ['title', 'sub_title', 'serial_number']
        select_params = ('active,'
                         'slug,'
                         'key,'
                         'name,'
                         'number,'
                         'type%7B%7D,'
                         'branches%7B%7D,'
                         'issuingAgency%7B%7D,'
                         'issueDate,'
                         'effectiveDate,'
                         'expiryDate,'
                         'gazetteNumber,'
                         'gazetteDate,'
                         'createdAt')
        date_format = '%Y-%m-%d'
        max_page = 2
        threshold = 0.8
        found = False
        query_params = {
            'target': 'document',
            'sort': 'keyword',
            'limit': 5,
            'select': select_params
        }
        if vbpl.issuance_date is not None:
            query_params['issueDateFrom'] = convert_datetime_to_str(vbpl.issuance_date)
        if vbpl.effective_date is not None:
            query_params['effectiveDateFrom'] = convert_datetime_to_str(vbpl.effective_date)
        if vbpl.expiration_date is not None:
            query_params['expiryDateFrom'] = convert_datetime_to_str(vbpl.expiration_date)

        for key in key_type:
            if found:
                break
            search_key = getattr(vbpl, key)
            if search_key is None:
                continue
            query_params['key'] = quote(search_key)
            for i in range(max_page):
                if found:
                    break
                query_params['page'] = i + 1
                params = concetti_query_params_url_encode(query_params)
                try:
                    async with aiohttp.ClientSession(trust_env=True) as session:
                        async with session.request('GET',
                                                   yarl.URL(f'{cls._concetti_base_url + search_url}?{params}',
                                                            encoded=True),
                                                   headers=cls.get_headers()
                                                   ) as resp:
                            await resp.text()
                    if resp.status == HTTPStatus.OK:
                        raw_json = await resp.json()
                        result_items = raw_json['items']

                        if len(result_items) == 0:
                            continue

                        for item in result_items:
                            # if the search result is similar to the source vbpl
                            if (Levenshtein.ratio(search_key, item['name']) >= threshold
                                    or Levenshtein.ratio(search_key, item['number']) >= threshold
                                    or Levenshtein.ratio(search_key, item['key']) >= threshold):

                                # Update effective date, expiry date and state of vbpl
                                effective_date_str = item['effectiveDate']
                                expiry_date_str = item['expiryDate']
                                if effective_date_str is not None:
                                    effective_date = datetime.strptime(effective_date_str, date_format)
                                    vbpl.effective_date = effective_date
                                    if effective_date > datetime.now():
                                        vbpl.state = 'Chưa có hiệu lực'
                                    else:
                                        if expiry_date_str is None:
                                            vbpl.state = 'Có hiệu lực'
                                        else:
                                            expiry_date = datetime.strptime(expiry_date_str, date_format)
                                            vbpl.expiration_date = expiry_date
                                            if expiry_date < datetime.now():
                                                vbpl.state = 'Hết hiệu lực'
                                            else:
                                                vbpl.state = 'Có hiệu lực'

                                # fetch pdf if needed
                                if vbpl.org_pdf_link is None or vbpl.org_pdf_link.strip() == '':
                                    slug = item['slug']
                                    doc_url = '/documents/slug'
                                    try:
                                        async with aiohttp.ClientSession(trust_env=True) as session:
                                            async with session.request('GET',
                                                                       f'{cls._concetti_base_url + doc_url}/{slug}',
                                                                       headers=cls.get_headers()
                                                                       ) as doc_resp:
                                                await doc_resp.text()
                                        if resp.status == HTTPStatus.OK:
                                            raw_doc_json = await doc_resp.json()
                                            pdf_id = raw_doc_json['pdfFile']
                                            if pdf_id is not None:
                                                pdf_url = f'{cls._concetti_base_url}/files/{pdf_id}/fetch'
                                                vbpl.org_pdf_link = pdf_url
                                                vbpl.file_link = get_document(pdf_url, True, pdf_id, True)
                                    except Exception as e:
                                        _logger.exception(f'Get concetti {slug} {e}')
                                        raise CommonException(500, 'Get concetti')

                                found = True
                                break
                except Exception as e:
                    _logger.exception(f'Search using concetti {e}')
                    raise CommonException(500, 'Search using concetti')

    # additional html crawl from tvpl
    @classmethod
    async def additional_html_crawl(cls, vbpl: Vbpl):
        search_url = '/page/tim-van-ban.aspx'
        key_type = ['title', 'sub_title', 'serial_number']
        threshold = 0.8
        found = False
        results = []
        vbpl_sub_parts = None

        for key in key_type:
            if found:
                break

            if getattr(vbpl, key) is None:
                continue

            search_key = getattr(vbpl, key)
            query_params = {
                'keyword': search_key,
                'sort': 1,
            }
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.request('GET',
                                               cls._tvpl_base_url + search_url,
                                               params=query_params,
                                               headers=cls.get_headers()
                                               ) as resp:
                        await resp.text()
            except Exception as e:
                _logger.exception(f'Search tvpl {e}')
                raise CommonException(500, 'Search tvpl')
            if resp.status == HTTPStatus.OK:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                search_results = soup.find_all('p', {'class': 'nqTitle'})

                for result in search_results:
                    search_text = get_html_node_text(result)
                    if Levenshtein.ratio(search_text, search_key) >= threshold:
                        found = True
                        result_url = result.find('a').get('href')
                        try:
                            async with aiohttp.ClientSession(trust_env=True) as session:
                                async with session.request('GET',
                                                           result_url,
                                                           headers=cls.get_headers()
                                                           ) as full_text_resp:
                                    await full_text_resp.text()
                            if full_text_resp.status == HTTPStatus.OK:
                                full_text_soup = BeautifulSoup(await full_text_resp.text(), 'lxml')
                                full_text = full_text_soup.find('div', {'class': 'cldivContentDocVn'})

                                if full_text is None:
                                    return None

                                vbpl.html = str(full_text)

                                lines = full_text.find_all('p')
                                if len(lines) == 0:
                                    lines = full_text.find_all('div')
                                results, vbpl_sub_parts = cls.process_html_full_text(vbpl, lines)
                            break
                        except Exception as e:
                            _logger.exception(f'Get tvpl html {result_url} {e}')
                            raise CommonException(500, 'Get tvpl html')
        return results, vbpl_sub_parts

    # get vbpl pdf from Download Tab
    @classmethod
    async def crawl_vbpl_pdf(cls, vbpl: Vbpl, vbpl_type: VbplType):
        # the download Tab is embedded in any link that does not return null
        # unfortunately any link relate to vbpl can return null so we need to check all of them
        # and i will say it again, this web is retarded
        if vbpl_type == VbplType.PHAP_QUY:
            possible_path = [
                VbplTab.FULL_TEXT.value,
                VbplTab.ATTRIBUTE.value,
                VbplTab.RELATED_DOC.value,
                VbplTab.DOC_MAP.value
            ]
        else:
            possible_path = [
                VbplTab.FULL_TEXT_HOP_NHAT.value,
                VbplTab.FULL_TEXT_HOP_NHAT_2.value,
                VbplTab.ATTRIBUTE_HOP_NHAT.value,
                VbplTab.DOC_MAP_HOP_NHAT.value
            ]

        for path in possible_path:
            aspx_url = f'/TW/Pages/vbpq-{path}.aspx'
            query_params = {
                'ItemID': vbpl.id
            }

            try:
                resp = await cls.call(method='GET', url_path=aspx_url, query_params=query_params)
                if resp.status == HTTPStatus.OK:
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    files = soup.find('ul', {'class': 'fileAttack'})
                    if files is not None:
                        file_urls = []
                        file_links = files.find_all('li')

                        for link in file_links:
                            link_node = link.find_all('a')[0]
                            link_content = get_html_node_text(link_node)
                            if re.search('.+.pdf', link_content) \
                                    or re.search('.+.doc', link_content) \
                                    or re.search('.+.docx', link_content):
                                href = link_node['href']
                                if re.search('javascript:downloadfile', href):
                                    file_url = href[len('javascript:downloadfile('):-2].split(',')[1][1:-1]
                                    file_urls.append(quote(setting.VBPL_PDF_BASE_URL + file_url, safe='/:?'))

                        if len(file_urls) > 0:
                            local_links = []
                            for url in file_urls:
                                doc_link = get_document(url, True)
                                if doc_link is not None:
                                    local_links.append(get_document(url, True))
                            if len(local_links) > 0:
                                vbpl.file_link = ' '.join(local_links)
                            vbpl.org_pdf_link = ' '.join(file_urls)
                        break

            except Exception as e:
                _logger.exception(f"Crawl vbpl pdf {vbpl.id}: {e}")
                raise CommonException(500, f"Crawl vbpl pdf")

    @classmethod
    async def crawl_vbpl_by_id(cls, vbpl_id, vbpl_type: VbplType):
        new_vbpl = Vbpl(
            id=vbpl_id,
        )
        if vbpl_type == VbplType.HOP_NHAT:
            await cls.crawl_vbpl_hopnhat_info(new_vbpl)
            await cls.crawl_vbpl_pdf(new_vbpl, vbpl_type)
            await cls.crawl_vbpl_hopnhat_fulltext(new_vbpl)
            await cls.search_concetti(new_vbpl)
            await cls.enrich_vbpl_sector(new_vbpl)
            vbpl_fulltext, vbpl_sub_part = await cls.additional_html_crawl(new_vbpl)
        else:
            await cls.crawl_vbpl_phapquy_info(new_vbpl)
            await cls.crawl_vbpl_pdf(new_vbpl, vbpl_type)
            vbpl_fulltext, vbpl_sub_part = await cls.crawl_vbpl_phapquy_fulltext(new_vbpl)
            await cls.search_concetti(new_vbpl)
            await cls.enrich_vbpl_sector(new_vbpl)
        await cls.push_vbpl_to_db(vbpl_id, new_vbpl, vbpl_fulltext, vbpl_sub_part)

    @classmethod
    async def fetch_vbpl_by_id(cls, vbpl_id):
        with LocalSession.begin() as session:
            vbpl_info = session.query(
                Vbpl.id,
                Vbpl.file_link,
                Vbpl.title,
                Vbpl.doc_type,
                Vbpl.serial_number,
                Vbpl.issuance_date,
                Vbpl.effective_date,
                Vbpl.expiration_date,
                Vbpl.gazette_date,
                Vbpl.state,
                Vbpl.issuing_authority,
                Vbpl.applicable_information,
                Vbpl.org_pdf_link,
                Vbpl.sub_title,
                Vbpl.sector
            ).filter(Vbpl.id == vbpl_id).order_by(Vbpl.updated_at.desc()).first()

            vbpl_related_document_info = session.query(VbplRelatedDocument.related_id, Vbpl.title, Vbpl.sub_title). \
                join(Vbpl, VbplRelatedDocument.related_id == Vbpl.id). \
                filter(VbplRelatedDocument.source_id == vbpl_id). \
                all()

            vbpl_doc_map_info = session.query(VbplDocMap.doc_map_id, Vbpl.title, Vbpl.sub_title). \
                join(Vbpl, VbplDocMap.doc_map_id == Vbpl.id). \
                filter(VbplDocMap.source_id == vbpl_id). \
                all()

        formatted_vbpl_info = (
            f"ID văn bản: {vbpl_info.id},\n"
            f"Đường dẫn lưu file: {vbpl_info.file_link},\n"
            f"Tiêu đề văn bản: {vbpl_info.title},\n"
            f"Loại văn bản: {vbpl_info.doc_type},\n"
            f"Số ký hiệu: {vbpl_info.serial_number},\n"
            f"Ngày ban hành: {vbpl_info.issuance_date},\n"
            f"Ngày có hiệu lực: {vbpl_info.effective_date},\n"
            f"Ngày hết hiệu lực: {vbpl_info.expiration_date},\n"
            f"Ngày đăng công báo: {vbpl_info.gazette_date},\n"
            f"Trạng thái: {vbpl_info.state},\n"
            f"Cơ quan ban hành/ Chức danh / Người ký: {vbpl_info.issuing_authority},\n"
            f"Thông tin áp dụng: {vbpl_info.applicable_information},\n"
            f"Đường dẫn đến văn bản gốc: {vbpl_info.org_pdf_link},\n"
            f"Tiêu đề phụ: {vbpl_info.sub_title},\n"
            f"Lĩnh vực: {vbpl_info.sector},\n"
        )
        print(formatted_vbpl_info)

        print("Thông tin các văn bản liên quan: ")
        for related_doc in vbpl_related_document_info:
            formatted_related_doc = (
                f"ID văn bản liên quan: {related_doc.related_id},\n"
                f"Tiêu đề văn bản liên quan: {related_doc.title},\n"
                f"Tiêu đề phụ văn bản liên quan: {related_doc.sub_title},\n"
            )
            print(formatted_related_doc)

        print("Thông tin các lược đồ: ")
        for doc in vbpl_doc_map_info:
            formatted_related_doc = (
                f"ID lược đồ: {doc.doc_map_id},\n"
                f"Tiêu đề lược đồ: {doc.title},\n"
                f"Tiêu đề phụ lược đồ: {doc.sub_title},\n"
            )
            print(formatted_related_doc)

        return vbpl_info, vbpl_related_document_info, vbpl_doc_map_info

    @classmethod
    async def get_vbpl_preview(cls, num_of_rows, issuance_date):
        target_date = convert_str_to_datetime(issuance_date)
        with LocalSession.begin() as session:
            query = session.query(Vbpl).filter(Vbpl.issuance_date == target_date).order_by(
                Vbpl.issuance_date.desc()).limit(num_of_rows)

        sql_folder_path = 'documents/preview/vbpl'
        os.makedirs(sql_folder_path, exist_ok=True)
        sql_file_path = os.path.join(sql_folder_path, 'vbpl_preview_script.sql')

        with open(sql_file_path, 'w') as dump_file:
            for vbpl_instance in query:
                values = []
                for column in Vbpl.__table__.columns.keys():
                    value = getattr(vbpl_instance, column)
                    if value is None:
                        values.append("NULL")
                    else:
                        values.append(f"'{value}'")
                values_str = ", ".join(values)
                insert_sql = f"INSERT INTO vbpl ({', '.join(Vbpl.__table__.columns.keys())}) VALUES ({values_str});"
                dump_file.write(insert_sql + '\n')

        file_links = []

        target_records = query.all()
        for record in target_records:
            if record.file_link is not None:
                file_links.append(record.file_link)

        output_rar_filepath = os.path.join(sql_folder_path, 'preview_vbpl.rar')
        with py7zr.SevenZipFile(output_rar_filepath, 'w') as archive:
            for file_link in file_links:
                archive.write(file_link)

    # get vbpl sector
    @classmethod
    async def enrich_vbpl_sector(cls, vbpl: Vbpl):
        if vbpl.serial_number == 'Không số':
            query_params = {
                'Keywords': vbpl.sub_title,
                'SearchOptions': 1,
                'SearchExact': 1
            }
        else:
            query_params = {
                'Keywords': vbpl.serial_number,
                'SearchOptions': 3,
                'SearchExact': 1
            }

        search_url = 'tim-van-ban.html'
        vbpl_sectors = []

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.request('GET',
                                           f'{cls._luat_vn_base_url + search_url}',
                                           params=query_params,
                                           headers=cls.get_headers()
                                           ) as resp:
                    await resp.text()
        except Exception as e:
            _logger.exception(f'Search vbpl on luatvietnam with url {search_url}')
            raise CommonException(500, 'Crawl vbpl sector from luatvietnam')

        if resp.status == HTTPStatus.OK:
            soup = BeautifulSoup(await resp.text(), 'lxml')
            search_results = soup.find_all('h2', {'class': 'doc-title'})
            result_url = ''
            # check if the searched doc is in the search result
            for search_result in search_results:
                title = search_result.find('a').get('title')
                if vbpl.serial_number in title or vbpl.sub_title in title:
                    result_url = search_result.find('a').get('href')
                    break
            # if not found, then stop the function, and mark those as "Lĩnh vực khác"
            if result_url == '':
                vbpl.sector = 'Lĩnh vực khác'
                return
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.request('GET',
                                               f'{cls._luat_vn_base_url + result_url}',
                                               params=query_params,
                                               headers=cls.get_headers()
                                               ) as vbpl_resp:
                        await vbpl_resp.text()
            except Exception as e:
                _logger.exception(f'Get vbpl info on luatvietnam with url {result_url}')
                raise CommonException(500, 'Crawl vbpl sector from luatvietnam')

            if vbpl_resp.status == HTTPStatus.OK:
                vbpl_soup = BeautifulSoup(await vbpl_resp.text(), 'lxml')
                summary = vbpl_soup.find('div', {'id': 'tomtat'})
                if summary is not None:
                    table_rows = summary.find_all('tr')
                    for row in table_rows:
                        sector_row = row.find('td', text="Lĩnh vực:")
                        if sector_row is None:
                            continue
                        all_sector_info = row.find_all('a')
                        for sector in all_sector_info:
                            vbpl_sector = sector.get('title')
                            # the sector above will be "Lĩnh vực: something", we need to remove "Lĩnh vực: "
                            colon_index = vbpl_sector.find(':')
                            if colon_index != -1:
                                # Extract the text after ':', removing any leading or trailing spaces
                                vbpl_sectors.append(vbpl_sector[colon_index + 1:].strip())

                        vbpl.sector = ' - '.join(vbpl_sectors)

        with LocalSession.begin() as session:
            # avoid upsert into 'Lĩnh vực khác' for the already specific sector
            check_sector = session.query(Vbpl).filter(Vbpl.id == vbpl.id).first()
            if check_sector is not None:
                if check_sector.sector != 'Lĩnh vực khác' and vbpl.sector is None:
                    vbpl.sector = check_sector.sector

        if vbpl.sector is None:
            vbpl.sector = 'Lĩnh vực khác'
