"""Narrow, validated qBittorrent connection and queue preferences."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

FIELDS = {
    'maxConnections': 'max_connec',
    'maxConnectionsPerTorrent': 'max_connec_per_torrent',
    'maxUploadSlots': 'max_uploads',
    'maxUploadSlotsPerTorrent': 'max_uploads_per_torrent',
    'maxActiveUploads': 'max_active_uploads',
    'maxActiveDownloads': 'max_active_downloads',
}


class TransferSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    maxConnections: int = Field(ge=-1, le=10000, strict=True)
    maxConnectionsPerTorrent: int = Field(ge=-1, le=10000, strict=True)
    maxUploadSlots: int = Field(ge=-1, le=1000, strict=True)
    maxUploadSlotsPerTorrent: int = Field(ge=-1, le=1000, strict=True)
    maxActiveUploads: int = Field(ge=-1, le=1000, strict=True)
    maxActiveDownloads: int = Field(ge=-1, le=100, strict=True)

    @field_validator('*')
    @classmethod
    def positive_or_unlimited(cls, value):
        if value == 0:
            raise ValueError('Choose a positive limit or unlimited (-1)')
        return value

    def preferences(self):
        values = self.model_dump()
        return {**{native: values[field] for field, native in FIELDS.items()},
                'queueing_enabled': True, 'max_active_torrents': -1}

    @classmethod
    def from_preferences(cls, preferences):
        values = {field: preferences.get(native) for field, native in FIELDS.items()}
        if preferences.get('queueing_enabled') is False:
            values.update(maxActiveUploads=-1, maxActiveDownloads=-1)
        return cls.model_validate(values)
