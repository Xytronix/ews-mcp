"""AttachmentService - Comprehensive attachment handling for EWS MCP v3.0."""

import logging
import io
from typing import Optional
from pathlib import Path

from ..core.attachment import Attachment, AttachmentContent, AttachmentType
from ..utils import safe_get
from ..attachment_parsers import extract_attachment_text


class AttachmentService:
    """
    Service for handling all attachment formats.

    Supports:
    - PDF, DOCX, Excel, PPTX, images (via Liteparse + OCR)
    - ZIP archives
    - Plain text
    - HTML
    """

    def __init__(self, ews_client):
        """
        Initialize AttachmentService.

        Args:
            ews_client: EWSClient instance
        """
        self.ews_client = ews_client
        self.logger = logging.getLogger(__name__)

    async def read_attachment(
        self,
        message_id: str,
        attachment_name: str,
        extract_text: bool = True,
        extract_images: bool = False,
        extract_metadata: bool = True,
        max_pages: int = 50
    ) -> Optional[AttachmentContent]:
        """
        Read and parse attachment content.

        Args:
            message_id: Parent message ID
            attachment_name: Name of attachment to read
            extract_text: Extract text content
            extract_images: Extract images
            extract_metadata: Extract metadata
            max_pages: Maximum pages for PDF

        Returns:
            AttachmentContent object or None
        """
        self.logger.info(f"Reading attachment: {attachment_name} from message {message_id}")

        try:
            # Get message
            message = await self._get_message(message_id)
            if not message:
                self.logger.error(f"Message not found: {message_id}")
                return None

            # Find attachment
            attachments = safe_get(message, 'attachments', []) or []
            target_attachment = None

            for att in attachments:
                if safe_get(att, 'name') == attachment_name:
                    target_attachment = att
                    break

            if not target_attachment:
                self.logger.error(f"Attachment not found: {attachment_name}")
                return None

            # Create Attachment metadata
            attachment = Attachment.from_ews_attachment(target_attachment, message_id)

            # Get content
            content = safe_get(target_attachment, 'content')
            if not content:
                return AttachmentContent(
                    attachment=attachment,
                    success=False,
                    error="No content available"
                )

            # Parse based on type
            ext = (attachment.extension or '').lstrip('.').lower()
            if ext:
                return await self._parse_with_extractors(
                    attachment, content, ext, extract_text, max_pages
                )

            # Default: return raw content info
            return AttachmentContent(
                attachment=attachment,
                format=attachment.extension or 'unknown',
                success=True,
                warnings=[f"Parser not available for {attachment.extension}"]
            )

        except Exception as e:
            self.logger.error(f"Failed to read attachment: {e}")
            return None

    async def _get_message(self, message_id: str):
        """Get message from any folder."""
        for folder_name in ['inbox', 'sent', 'drafts']:
            folder = getattr(self.ews_client.account, folder_name, None)
            if folder is None:
                continue
            try:
                message = folder.get(id=message_id)
                if message:
                    return message
            except Exception as e:
                self.logger.debug(f"Message not in {folder_name}: {e}")
                continue
        return None

    async def _parse_with_extractors(
        self,
        attachment: Attachment,
        content: bytes,
        ext: str,
        extract_text: bool,
        max_pages: int,
    ) -> AttachmentContent:
        """Parse attachments via the shared read_attachment extractors."""
        import asyncio

        result = AttachmentContent(attachment=attachment, format=ext)
        try:
            if extract_text:
                result.text = await asyncio.to_thread(
                    extract_attachment_text,
                    content,
                    ext,
                    max_pages=max_pages,
                    extract_tables=True,
                )
            result.success = True
            return result
        except Exception as e:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error=f"Failed to parse attachment: {e}",
            )

    async def _parse_text(
        self,
        attachment: Attachment,
        content: bytes
    ) -> AttachmentContent:
        """Parse plain text file."""
        try:
            text = content.decode('utf-8')
            return AttachmentContent(
                attachment=attachment,
                format='text',
                text=text,
                success=True
            )
        except Exception as e:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error=f"Failed to parse text: {str(e)}"
            )

    async def _parse_csv(
        self,
        attachment: Attachment,
        content: bytes
    ) -> AttachmentContent:
        """Parse CSV file."""
        try:
            import csv

            result = AttachmentContent(attachment=attachment, format='csv')

            text = content.decode('utf-8')
            reader = csv.DictReader(io.StringIO(text))

            result.structured_data = list(reader)
            result.success = True
            return result

        except Exception as e:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error=f"Failed to parse CSV: {str(e)}"
            )

    async def _parse_pptx(
        self,
        attachment: Attachment,
        content: bytes,
        extract_text: bool,
        extract_images: bool
    ) -> AttachmentContent:
        """Parse PPTX file."""
        try:
            from pptx import Presentation

            result = AttachmentContent(attachment=attachment, format='pptx')

            prs = Presentation(io.BytesIO(content))

            # Extract text from slides
            if extract_text:
                slides_text = []
                for i, slide in enumerate(prs.slides):
                    slide_text = f"--- Slide {i+1} ---\n"
                    for shape in slide.shapes:
                        if hasattr(shape, "text"):
                            slide_text += shape.text + "\n"
                    slides_text.append(slide_text)

                result.text = '\n\n'.join(slides_text)

            # Metadata
            result.metadata = {
                'slides': len(prs.slides),
                'author': prs.core_properties.author,
                'title': prs.core_properties.title,
            }

            result.success = True
            return result

        except ImportError:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error="python-pptx not installed. Install with: pip install python-pptx"
            )
        except Exception as e:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error=f"Failed to parse PPTX: {str(e)}"
            )

    async def _parse_zip(
        self,
        attachment: Attachment,
        content: bytes
    ) -> AttachmentContent:
        """Parse ZIP archive."""
        try:
            import zipfile

            result = AttachmentContent(attachment=attachment, format='zip')

            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # List files
                result.files = []
                for info in zf.infolist():
                    result.files.append({
                        'name': info.filename,
                        'size': info.file_size,
                        'compressed_size': info.compress_size,
                        'modified': str(info.date_time),
                    })

                # Extract readable text files
                result.extracted_content = {}
                for file_info in zf.infolist():
                    if file_info.filename.endswith(('.txt', '.md', '.csv', '.json')):
                        try:
                            with zf.open(file_info) as f:
                                content_text = f.read().decode('utf-8')
                                result.extracted_content[file_info.filename] = content_text
                        except Exception as e:
                            self.logger.debug(f"Skipped {file_info.filename}: {e}")

            result.success = True
            return result

        except Exception as e:
            return AttachmentContent(
                attachment=attachment,
                success=False,
                error=f"Failed to parse ZIP: {str(e)}"
            )
