# app/routers/assets.py

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from typing import List
from sqlalchemy import or_
from decimal import Decimal

from app.db.database import SessionLocal
from app.models.portfolio import PortfolioHoldings, Portfolio
from app.schemas.asset import (
    AssetRead,
    AssetCreate,
    AssetBase,
    AssetPageResponse,
)
from app.schemas.financial_product import FinancialProductRead, SectorInfo
from app.models.financial_product import FinancialProducts
from app.models.transaction import TransactionHistory

router = APIRouter(prefix="/assets", tags=["Assets"])


def get_db():
    """
    요청마다 DB 세션 생성/해제
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get(
    "/",
    response_model=AssetPageResponse,
    summary="특정 포트폴리오의 보유 자산 조회 (페이징 지원)",
    responses={
        200: {"description": "자산 목록을 성공적으로 조회함."},
        400: {"description": "잘못된 요청 파라미터."},
        404: {"description": "포트폴리오를 찾을 수 없음."},
    },
)
def read_assets(
    portfolio_id: int = Query(..., description="조회할 포트폴리오 ID"),
    page: int = Query(1, ge=1, description="페이지 번호 (1부터 시작)"),
    per_page: int = Query(
        10, ge=1, le=100, description="페이지당 표시할 개수 (최대 100)"
    ),
    db: Session = Depends(get_db),
):
    """
    **특정 포트폴리오의 보유 자산 목록을 조회하는 API (페이징 지원).**

    - 특정 포트폴리오 ID에 속하는 자산들만 조회합니다.
    - `financial_product_id` 대신 `financial_product` 객체를 포함하여 반환합니다.
    - `currency_code`, `price`, `quantity` 등의 정보를 제공합니다.

    **Query Parameters:**
    - **portfolio_id**: 조회할 포트폴리오 ID
    - **page**: 페이지 번호 (1부터 시작)
    - **per_page**: 페이지당 표시할 개수 (최대 100)

    **Response:**
    - `200 OK`: 성공적으로 자산 목록을 반환
    - `400 Bad Request`: 요청이 잘못되었을 경우
    - `404 Not Found`: 해당 포트폴리오를 찾을 수 없는 경우
    """
    # 포트폴리오 존재 여부 확인
    portfolio = db.query(Portfolio).filter(Portfolio.portfolio_id == portfolio_id).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="포트폴리오를 찾을 수 없습니다.")
    
    offset = (page - 1) * per_page
    
    # 특정 포트폴리오에 속한 자산만 필터링
    total = db.query(PortfolioHoldings).filter(
        PortfolioHoldings.portfolio_id == portfolio_id
    ).count()

    holdings_query = db.query(PortfolioHoldings).filter(
        PortfolioHoldings.portfolio_id == portfolio_id
    ).offset(offset).limit(per_page).all()

    # financial_product 정보를 포함하여 응답 리스트 구성
    results = [
        AssetRead(
            portfolio_id=holding.portfolio_id,
            currency_code=holding.currency_code,
            price=holding.price,
            quantity=holding.quantity,
            financial_product=FinancialProductRead(
                financial_product_id=holding.financial_product.financial_product_id,
                product_name=holding.financial_product.product_name,
                ticker=holding.financial_product.ticker,
                sector=SectorInfo(
                    sector_id=holding.financial_product.sector.sector_id,
                    sector_name=holding.financial_product.sector.sector_name,
                ),
            ),
        )
        for holding in holdings_query
    ]

    return AssetPageResponse(total=total, page=page, per_page=per_page, assets=results)


@router.post(
    "/",
    response_model=AssetRead,
    summary="보유 자산 추가 및 거래 기록",
    responses={
        201: {"description": "자산과 거래가 성공적으로 추가됨."},
        400: {"description": "잘못된 요청."},
    },
)
def create_asset_and_transaction(asset_data: AssetCreate, db: Session = Depends(get_db)):
    """
    **새로운 보유 자산과 거래 기록을 추가하는 API.**

    - 요청 본문에 `portfolio_id`, `financial_product_id`, `currency_code`, `price`, `quantity`, `transaction_type`, `transaction_date`를 포함해야 합니다.
    """
    existing = (
        db.query(PortfolioHoldings)
        .filter_by(
            portfolio_id=asset_data.portfolio_id,
            financial_product_id=asset_data.financial_product_id,
        )
        .first()
    )
    
    if existing:
        # 요청한 화폐 단위가 기존 화폐 단위와 다른 경우 에러 발생
        if existing.currency_code != asset_data.currency_code:
            raise HTTPException(status_code=400, detail="요청한 화폐 단위가 기존 화폐 단위와 다릅니다.")
        
        if asset_data.transaction_type == "판매":
            purchase_price = existing.price
            profit_rate = ((Decimal(asset_data.price) - purchase_price) / purchase_price) * 100

            sale_quantity = Decimal(asset_data.quantity)
            if existing.quantity < sale_quantity:
                raise HTTPException(status_code=400, detail="판매 수량이 현재 보유 중인 자산의 수량보다 많습니다.")
            
            existing.quantity -= sale_quantity

            new_transaction = TransactionHistory(
                portfolio_id=asset_data.portfolio_id,
                financial_product_id=asset_data.financial_product_id,
                transaction_type=asset_data.transaction_type,
                price=Decimal(asset_data.price),
                quantity=sale_quantity,
                created_at=asset_data.transaction_date,
                currency_code=existing.currency_code,
                profit_rate=profit_rate,
            )
            db.add(new_transaction)
            
            # 미리 필요한 데이터를 저장
            fp = existing.financial_product
            portfolio_id = existing.portfolio_id
            currency_code = existing.currency_code
            price = existing.price

            # 보유량 0인 경우 삭제 후 응답 반환
            if existing.quantity == 0:
                asset_read = AssetRead(
                    portfolio_id=portfolio_id,
                    currency_code=currency_code,
                    price=price,
                    quantity=Decimal("0"),
                    financial_product=FinancialProductRead(
                        financial_product_id=fp.financial_product_id,
                        product_name=fp.product_name,
                        ticker=fp.ticker,
                        sector=SectorInfo(
                            sector_id=fp.sector.sector_id,
                            sector_name=fp.sector.sector_name,
                        ),
                    ),
                )
                db.delete(existing)
                db.commit()
                return asset_read
            else:
                db.commit()
                db.refresh(existing)
                return AssetRead(
                    portfolio_id=existing.portfolio_id,
                    currency_code=existing.currency_code,
                    price=existing.price,
                    quantity=existing.quantity,
                    financial_product=FinancialProductRead(
                        financial_product_id=existing.financial_product.financial_product_id,
                        product_name=existing.financial_product.product_name,
                        ticker=existing.financial_product.ticker,
                        sector=SectorInfo(
                            sector_id=existing.financial_product.sector.sector_id,
                            sector_name=existing.financial_product.sector.sector_name,
                        ),
                    ),
                )

        else:  # 구매 처리
            profit_rate = None
            new_quantity = Decimal(asset_data.quantity)
            new_price = Decimal(asset_data.price)

            total_value = (existing.price * existing.quantity) + (new_price * new_quantity)
            existing.quantity += new_quantity
            existing.price = total_value / existing.quantity

            new_transaction = TransactionHistory(
                portfolio_id=asset_data.portfolio_id,
                financial_product_id=asset_data.financial_product_id,
                transaction_type=asset_data.transaction_type,
                price=Decimal(asset_data.price),
                quantity=new_quantity,
                created_at=asset_data.transaction_date,
                currency_code=existing.currency_code,
                profit_rate=profit_rate,
            )
            db.add(new_transaction)
            db.commit()
            db.refresh(existing)

            return AssetRead(
                portfolio_id=existing.portfolio_id,
                currency_code=existing.currency_code,
                price=existing.price,
                quantity=existing.quantity,
                financial_product=FinancialProductRead(
                    financial_product_id=existing.financial_product.financial_product_id,
                    product_name=existing.financial_product.product_name,
                    ticker=existing.financial_product.ticker,
                    sector=SectorInfo(
                        sector_id=existing.financial_product.sector.sector_id,
                        sector_name=existing.financial_product.sector.sector_name,
                    ),
                ),
            )

    # 신규 자산 생성
    new_asset = PortfolioHoldings(
        portfolio_id=asset_data.portfolio_id,
        financial_product_id=asset_data.financial_product_id,
        currency_code=asset_data.currency_code,
        price=asset_data.price,
        quantity=asset_data.quantity,
    )
    db.add(new_asset)

    new_transaction = TransactionHistory(
        portfolio_id=asset_data.portfolio_id,
        financial_product_id=asset_data.financial_product_id,
        transaction_type=asset_data.transaction_type,
        price=Decimal(asset_data.price),
        quantity=asset_data.quantity,
        created_at=asset_data.transaction_date,
        currency_code=new_asset.currency_code,
    )
    db.add(new_transaction)

    db.commit()
    db.refresh(new_asset)
    db.refresh(new_transaction)

    return AssetRead(
        portfolio_id=new_asset.portfolio_id,
        currency_code=new_asset.currency_code,
        price=new_asset.price,
        quantity=new_asset.quantity,
        financial_product=FinancialProductRead(
            financial_product_id=new_asset.financial_product.financial_product_id,
            product_name=new_asset.financial_product.product_name,
            ticker=new_asset.financial_product.ticker,
            sector=SectorInfo(
                sector_id=new_asset.financial_product.sector.sector_id,
                sector_name=new_asset.financial_product.sector.sector_name,
            ),
        ),
    )


@router.patch(
    "/transfer",
    response_model=AssetRead,
    summary="보유 자산 다른 포트폴리오로 전송",
    responses={
        200: {"description": "자산이 성공적으로 전송됨."},
        400: {"description": "잘못된 요청 데이터."},
        404: {"description": "자산이나 포트폴리오를 찾을 수 없음."},
    },
)
def update_assets(
    source_portfolio_id: int = Body(..., description="원본 포트폴리오 ID"),
    financial_product_id: int = Body(..., description="전송할 금융 상품 ID"),
    target_portfolio_id: int = Body(..., description="대상 포트폴리오 ID"),
    db: Session = Depends(get_db),
):
    """
    **보유 자산을 다른 포트폴리오로 전송하는 API.**

    - `source_portfolio_id`, `financial_product_id`를 기준으로 자산을 찾고, `target_portfolio_id` 포트폴리오로 전송합니다.
    - 원본 자산은 삭제되고, 대상 포트폴리오에 자산이 추가됩니다.
    - 동일한 자산이 대상 포트폴리오에 이미 존재하는 경우, 수량이 합산됩니다.

    **Response:**
    - `200 OK`: 성공적으로 자산이 전송됨
    - `400 Bad Request`: 요청이 잘못되었을 경우
    - `404 Not Found`: 자산이나 포트폴리오를 찾을 수 없음
    """
    # 소스 포트폴리오 확인
    source_portfolio = db.query(Portfolio).filter(Portfolio.portfolio_id == source_portfolio_id).first()
    if not source_portfolio:
        raise HTTPException(status_code=404, detail="원본 포트폴리오를 찾을 수 없습니다.")

    # 타겟 포트폴리오 확인
    target_portfolio = db.query(Portfolio).filter(Portfolio.portfolio_id == target_portfolio_id).first()
    if not target_portfolio:
        raise HTTPException(status_code=404, detail="대상 포트폴리오를 찾을 수 없습니다.")
    
    # 동일한 포트폴리오인 경우 오류 반환
    if source_portfolio_id == target_portfolio_id:
        raise HTTPException(status_code=400, detail="원본과 대상 포트폴리오가 동일합니다.")

    # 원본 자산 찾기
    source_asset = (
        db.query(PortfolioHoldings)
        .filter_by(
            portfolio_id=source_portfolio_id,
            financial_product_id=financial_product_id,
        )
        .first()
    )

    if not source_asset:
        raise HTTPException(status_code=404, detail="전송할 자산을 찾을 수 없습니다.")

    # 대상 포트폴리오에 같은 자산이 있는지 확인
    target_asset = (
        db.query(PortfolioHoldings)
        .filter_by(
            portfolio_id=target_portfolio_id,
            financial_product_id=financial_product_id,
        )
        .first()
    )

    if target_asset:
        # 이미 존재하는 경우, 수량 합산 및 평균 가격 계산
        total_value = (target_asset.price * target_asset.quantity) + (source_asset.price * source_asset.quantity)
        target_asset.quantity += source_asset.quantity
        target_asset.price = total_value / target_asset.quantity
        
        # 원본 자산 삭제
        db.delete(source_asset)
        db.commit()
        db.refresh(target_asset)
        
        return AssetRead(
            portfolio_id=target_asset.portfolio_id,
            currency_code=target_asset.currency_code,
            price=target_asset.price,
            quantity=target_asset.quantity,
            financial_product=FinancialProductRead(
                financial_product_id=target_asset.financial_product.financial_product_id,
                product_name=target_asset.financial_product.product_name,
                ticker=target_asset.financial_product.ticker,
                sector=SectorInfo(
                    sector_id=target_asset.financial_product.sector.sector_id,
                    sector_name=target_asset.financial_product.sector.sector_name,
                ),
            ),
        )
    else:
        # 대상 포트폴리오에 자산이 없는 경우, 새로 생성
        new_asset = PortfolioHoldings(
            portfolio_id=target_portfolio_id,
            financial_product_id=financial_product_id,
            currency_code=source_asset.currency_code,
            price=source_asset.price,
            quantity=source_asset.quantity,
        )
        db.add(new_asset)
        
        # 원본 자산 삭제
        db.delete(source_asset)
        db.commit()
        db.refresh(new_asset)
        
        return AssetRead(
            portfolio_id=new_asset.portfolio_id,
            currency_code=new_asset.currency_code,
            price=new_asset.price,
            quantity=new_asset.quantity,
            financial_product=FinancialProductRead(
                financial_product_id=new_asset.financial_product.financial_product_id,
                product_name=new_asset.financial_product.product_name,
                ticker=new_asset.financial_product.ticker,
                sector=SectorInfo(
                    sector_id=new_asset.financial_product.sector.sector_id,
                    sector_name=new_asset.financial_product.sector.sector_name,
                ),
            ),
        )


@router.delete(
    "/",
    summary="보유 자산 삭제",
    responses={
        200: {"description": "자산이 성공적으로 삭제됨."},
        400: {"description": "잘못된 요청 데이터."},
    },
)
def delete_assets(
    assets_to_delete: List[AssetBase] = Body(...), db: Session = Depends(get_db)
):
    """
    **보유 자산을 삭제하는 API.**

    - `portfolio_id`, `financial_product_id`를 기준으로 해당 자산을 삭제합니다.

    **Response:**
    - `200 OK`: 성공적으로 삭제됨
    - `400 Bad Request`: 요청이 잘못되었을 경우
    """
    for asset_data in assets_to_delete:
        target = (
            db.query(PortfolioHoldings)
            .filter_by(
                portfolio_id=asset_data.portfolio_id,
                financial_product_id=asset_data.financial_product_id,
            )
            .first()
        )
        if target:
            db.delete(target)

    db.commit()
    return {"detail": "선택된 보유 자산이 삭제되었습니다."}


@router.get(
    "/search",
    response_model=List[FinancialProductRead],
    summary="금융 상품 검색",
    responses={
        200: {"description": "검색 결과를 성공적으로 반환"},
        400: {"description": "잘못된 검색어"},
    },
)
def search_financial_products(
    query: str = Query(..., min_length=1, description="검색어 (티커 또는 상품명)"),
    db: Session = Depends(get_db),
):
    """
    **금융 상품을 티커 또는 상품명으로 검색하는 API.**

    - 검색어는 대소문자를 구분하지 않습니다.
    - 부분 일치도 검색됩니다.
    - 티커와 상품명 모두에서 검색합니다.

    **Parameters:**
    - `query`: 검색할 티커 또는 상품명 (최소 1자 이상)

    **Returns:**
    - 검색 조건과 일치하는 금융 상품 목록
    """
    if not query:
        raise HTTPException(
            status_code=400,
            detail="검색어를 입력해주세요"
        )

    # 대소문자 구분 없이 검색하기 위해 검색어를 소문자로 변환
    search = f"%{query.lower()}%"
    
    results = (
        db.query(FinancialProducts)
        .filter(
            or_(
                FinancialProducts.ticker.ilike(search),
                FinancialProducts.product_name.ilike(search)
            )
        )
        .all()
    )

    return [
        FinancialProductRead(
            financial_product_id=product.financial_product_id,
            product_name=product.product_name,
            ticker=product.ticker,
            sector=SectorInfo(
                sector_id=product.sector.sector_id,
                sector_name=product.sector.sector_name,
            ),
        )
        for product in results
    ]
