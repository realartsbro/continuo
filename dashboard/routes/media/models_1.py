from __future__ import annotations


class MediaTrimRequest(BaseModel):
    src: str
    start: float
    end: float
    dest: str
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    frame_accurate: bool = False

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_time_number(cls, value: Any) -> Any:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= value <= MAX_DERIVATION_TIME_SECONDS
        ):
            raise ValueError("Trim times must be finite JSON numbers within the supported range.")
        return value

    @field_validator("frame_accurate", mode="before")
    @classmethod
    def validate_frame_accurate_boolean(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("Frame-accurate mode must be a JSON boolean.")
        return value


class MediaPrepareRequest(BaseModel):
    src: str
    start: float
    end: float
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    in_frame: int | None = None
    out_frame: int | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_time_number(cls, value: Any) -> Any:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= value <= MAX_DERIVATION_TIME_SECONDS
        ):
            raise ValueError("Prepare times must be finite JSON numbers within the supported range.")
        return value

    @field_validator("in_frame", "out_frame", mode="before")
    @classmethod
    def validate_frame_integer(cls, value: Any) -> Any:
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_DERIVATION_FRAME
        ):
            raise ValueError("Prepared frame bounds must be JSON integers within the supported range.")
        return value


class MediaProbeRequest(BaseModel):
    path: str
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None


class MediaFrameRequest(BaseModel):
    path: str
    frame: int
    fps: float
    width: int = 320
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None

    @field_validator("frame", mode="before")
    @classmethod
    def validate_frame_integer(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_DERIVATION_FRAME:
            raise ValueError("Frame must be a JSON integer within the supported range.")
        return value

    @field_validator("fps", mode="before")
    @classmethod
    def validate_fps_number(cls, value: Any) -> Any:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < value <= MAX_DERIVATION_FPS
        ):
            raise ValueError("FPS must be a finite positive JSON number within the supported range.")
        return value

    @field_validator("width", mode="before")
    @classmethod
    def validate_width_integer(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Frame width must be a positive JSON integer.")
        return value


class MediaListRequest(BaseModel):
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None


class MediaThumbRequest(BaseModel):
    # Lane w22-cut (CUT-177): Cut's viewer names a folder still by its cut ref (file:<sha256>) and its project
    # instead of a path; the backend resolves it through the project's footage index (the path never leaves it).
    path: str = ""
    ref: str | None = Field(default=None, pattern=r"^file:[0-9a-f]{64}$")
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    size: int = 256
    # "data_url": JSON {data_url} for the desktop's JSON-only ctx.rest bridge.
    format: Literal["jpeg", "data_url"] = "jpeg"
    # Lane w39-p4-fix: a picture by path for a keyframe's Crop / Pad bake on the Video page: up to 2048 px (the renderer's
    # MAX_REFRAME_EDGE) instead of the 384 px thumbnail cap; never scaled up, never held in the memory cache.
    reframe: bool = False
