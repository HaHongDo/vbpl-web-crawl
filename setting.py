from pydantic import BaseSettings
import os
from dotenv import load_dotenv

load_dotenv(verbose=True)


class Setting(BaseSettings):
    ROOT_DIR = os.path.abspath(os.path.join(
        os.path.dirname(__file__)
    ))

    VBPl_BASE_URL: str = os.getenv('VBPL_BASE_URL')
    VBPL_PDF_BASE_URL: str = os.getenv('VBPL_PDF_BASE_URL')
    ANLE_BASE_URL: str = os.getenv('ANLE_BASE_URL')
    SQLALCHEMY_DATABASE_URI: str = os.getenv('SQLALCHEMY_DATABASE_URI')
    CONCETTI_BASE_URL: str = os.getenv('CONCETTI_BASE_URL')
    TVPL_BASE_URL: str = os.getenv('TVPL_BASE_URL')
    CONG_BAO_BASE_URL: str = os.getenv('CONG_BAO_BASE_URL')
    LUAT_VN_BASE_URL: str = os.getenv('LUAT_VN_BASE_URL')


setting = Setting()
