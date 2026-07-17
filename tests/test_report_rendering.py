from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from recruit_crawler.summarizer import render_report_v3, ReportRenderError
from recruit_crawler.report_policy import REPORT_TABLE_COLUMNS, REPORT_MAX_QUEUE_ROWS

def result(p): return SimpleNamespace(run_date=date(2026,7,14),command_mode='scheduled-run', **p)
def projection(queue=()): return {'summary':{'collected':len(queue),'apply_total':1,'hold_total':0,'manual_review_total':0,'exclude':0,'expired':0,'low_priority_total':0},'report_queue':queue,'action_queue':queue,'manual_queue':(),'gate_sources':()}
def row(**kw): return {'final_disposition':'apply','title':'공고','company':'회사','location':'서울','deadline':'2026-08-01','reason_codes':(),**kw}
class ReportRenderingTests(TestCase):
 def test_exact_eight_column_table_and_no_score_or_evidence(self):
  with patch('recruit_crawler.projection.project_pipeline_result',return_value=projection((row(score=99,matched_evidence=('secret',)),))):
   text=render_report_v3(result({})).markdown_bytes.decode()
  self.assertIn('| 순위 | 판정 | 공고 | 회사 | 지역 | 마감 | 사유 | 링크 |',text); self.assertNotIn('99',text); self.assertNotIn('secret',text)
 def test_safe_link_only(self):
  x=row(source_id='saramin',source_posting_id='123',source_detail_quality='verified',source_url='https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=123&rec_seq=0')
  with patch('recruit_crawler.projection.project_pipeline_result',return_value=projection((x,))): text=render_report_v3(result({})).markdown_bytes.decode()
  self.assertIn('[열기](<https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=123&rec_seq=0>)',text)
 def test_queue_capacity_fails_closed(self):
  q=tuple(row(title=str(i)) for i in range(REPORT_MAX_QUEUE_ROWS+1))
  with patch('recruit_crawler.projection.project_pipeline_result',return_value=projection(q)), self.assertRaises(ReportRenderError): render_report_v3(result({}))
 def test_labels(self):
  q=tuple(row(final_disposition=x) for x in ('apply','hold','manual_review','exclude'))
  with patch('recruit_crawler.projection.project_pipeline_result',return_value=projection(q)): text=render_report_v3(result({})).markdown_bytes.decode()
  for label in ('지원 추천','도전 지원','원문 확인 필요','제외'): self.assertIn(label,text)
