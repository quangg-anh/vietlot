from datetime import date
from pydantic import BaseModel, Field, model_validator

class DrawIn(BaseModel):
    draw_id: int = Field(gt=0)
    draw_date: date
    n1: int = Field(ge=1, le=55)
    n2: int = Field(ge=1, le=55)
    n3: int = Field(ge=1, le=55)
    n4: int = Field(ge=1, le=55)
    n5: int = Field(ge=1, le=55)
    n6: int = Field(ge=1, le=55)
    special: int = Field(ge=1, le=55)
    source_url: str = ""

    @model_validator(mode="after")
    def validate_numbers(self):
        main = [self.n1, self.n2, self.n3, self.n4, self.n5, self.n6]
        if len(set(main)) != 6:
            raise ValueError("Six main numbers must be unique")
        if self.special in main:
            raise ValueError("Special number must not duplicate a main number")
        if main != sorted(main):
            raise ValueError("Main numbers must be sorted ascending")
        return self

class BacktestRequest(BaseModel):
    min_train: int = Field(default=100, ge=20, le=10000)
    test_draws: int = Field(default=100, ge=1, le=10000)
    method: str = Field(default="bayesian", pattern="^(uniform|bayesian|xgboost)$")
