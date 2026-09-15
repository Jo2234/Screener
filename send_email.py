#!/usr/bin/env python3
"""
Send email with PDF attachment via Resend API.
API key is loaded from .email_config file.

To set up:
1. Sign up at https://resend.com (free tier: 100 emails/day)
2. Get your API key from the dashboard
3. Add it to .email_config
"""

import sys
import os
import base64
import json
from datetime import datetime
from pathlib import Path

try:
    import resend
except ImportError:
    print("ERROR: resend package not installed. Run: pip3 install resend")
    sys.exit(1)


def send_email(pdf_path: str):
    """Send email with PDF attachment via Resend."""
    
    # Load config from environment
    api_key = os.environ.get('RESEND_API_KEY')
    recipient_email = os.environ.get('RECIPIENT_EMAIL')
    cc_email = os.environ.get('CC_EMAIL')  # Optional CC recipient
    
    if not api_key:
        print("ERROR: RESEND_API_KEY not found in .email_config")
        print("\nTo set up Resend:")
        print("1. Sign up at https://resend.com (free)")
        print("2. Get your API key from the dashboard")
        print("3. Add to .email_config: export RESEND_API_KEY='re_xxxxxxxx'")
        sys.exit(1)
    
    if not recipient_email:
        print("ERROR: RECIPIENT_EMAIL not found in .email_config")
        sys.exit(1)
    
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        print(f"ERROR: PDF not found: {pdf_path}")
        sys.exit(1)
    
    # Scheduled runs require this sidecar in the wrapper. Older PDFs without a
    # sidecar can still be sent manually; any present sidecar must permit delivery.
    quality_notice = ""
    subject_suffix = ""
    summary_path = pdf_file.parent / 'run_summary.json'
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        if not (summary.get('email_allowed') and summary.get('gate_passed')
                and summary.get('report_generated') and summary.get('report_path') == pdf_file.name):
            raise SystemExit('ERROR: Data quality gate failed or report mismatch; email suppressed')
        if summary['status'] == 'partial':
            totals = summary['totals']
            subject_suffix = ' — Partial coverage'
            quality_notice = (f"<p><strong>Partial coverage:</strong> {totals['successful_symbols']} of "
                              f"{totals['total_symbols']} symbols were analyzed; {totals['failed_symbols']} "
                              "were unavailable. The configured data quality gate passed. "
                              "See the report for missing symbols, reasons and thresholds.</p>")

    # Extract date from filename
    pdf_name = pdf_file.name
    week_date = pdf_name.replace('EMA_Crossover_Report_', '').replace('.pdf', '')
    
    # Read and encode PDF
    with open(pdf_file, 'rb') as f:
        pdf_content = base64.b64encode(f.read()).decode('utf-8')
    
    # Configure Resend
    resend.api_key = api_key
    
    # Send email
    try:
        params = {
            "from": "Screener <onboarding@resend.dev>",  # Free tier uses this sender
            "to": [recipient_email],
            "subject": f"EMA Crossover Report - Week of {week_date}{subject_suffix}",
            "html": f"""
            <h2>Weekly EMA Crossover Report</h2>
            <p>Please find attached the EMA Crossover Screener report for the week ending <strong>{week_date}</strong>.</p>
            <p>This report was automatically generated on {datetime.now().strftime('%Y-%m-%d at %H:%M')}.</p>
            {quality_notice}
            <h3>Summary</h3>
            <ul>
                <li>78-day / 165-day EMA crossover analysis</li>
                <li>Markets scanned: China, ETFs, NSE, NYSE, Singapore</li>
            </ul>
            <p>Best regards,<br>Automated Screener</p>
            """,
            "attachments": [
                {
                    "filename": pdf_name,
                    "content": pdf_content,
                }
            ],
        }
        
        # Add CC if configured
        if cc_email:
            params["cc"] = [cc_email]
        
        email = resend.Emails.send(params)
        print(f"Email sent successfully to {recipient_email}")
        if cc_email:
            print(f"CC: {cc_email}")
        print(f"Email ID: {email['id']}")
        
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")
        sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python send_email.py <path_to_pdf>")
        sys.exit(1)
    
    send_email(sys.argv[1])
