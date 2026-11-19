# Danh mục trường đầy đủ của SKU master push v4.0.0

Contract đích, chưa phải payload runtime đã triển khai. Sinh từ code Master Data và types giao diện ngày 27/10/2026.

## Product

`id`, `skuCode`, `posCode`, `barcode`, `defaultBarcode`, `barcodes`, `barcodeCount`, `productName`, `shortName`, `unitName`, `costPrice`, `salePrice`, `netSalePrice`, `marginPercent`, `supplierCode`, `supplierName`, `supplierDisplayName`, `contractCode`, `spPrice`, `purchasePrice`, `categoryName`, `countryOrigin`, `status`, `statusCode`, `tthh`, `inTaxRate`, `inTaxCode`, `outTaxRate`, `outTaxCode`, `vatPercent`, `weightVolume`, `createdAt`, `suppliers`, `defaultSupplier`, `rawMasterData`, `contractDetails`, `ecommerce`, `pluCode`, `codeType`, `brandName`, `isConsign`, `isOwnBrand`, `categoryCode`, `deptId`, `deptName`, `declaredCategory`, `subcategoryName`, `statusLabel`, `allowPosDisplay`, `isLockedForTargetBranch`, `pricing`, `inventoryCttk`, `imageUrl`, `isOnWebsite`, `webTitle`, `webUrl`, `webPrice`, `packaging`, `conversionRate`, `mdPhuTrach`, `buyerPhuTrach`, `hsd`, `hstMin`, `hstMax`, `ttGom`, `isWeightItem`.

## Barcode

`barcode`, `isDefault`, `type`, `descript`, `unitSymb`, `unitConv`.

## Supplier

`supplierCode`, `supplierName`, `companyName`, `isDefault`, `spPrice`, `unit`, `vatIncluded`, `taxCodeVat`, `contractCode`, `buyerInCharge`, `taxCode`, `bankAccount`, `bankName`, `contactName`, `contactPhone`, `contactEmail`, `status`.

## Tthh

`maSanPham`, `mdPhuTrach`, `buyerPhuTrach`, `quyCach`, `hsd`, `hsdQuyDoi`, `hstMin`, `hstMax`, `ttGom`, `datVeKhoTong`, `datVeKhoTongRaw`, `nguyenNhan`, `ngayDoiTrangThai`, `ngayCanUpdateTiepTheo`, `maDeXuatDoiTrangThai`, `thoiGianDoiNguoiDat`, `pieceUnit`, `khongDauVao`, `khongCoDauVao`, `khoaRieng`, `minDisplay`, `trungBay`, `allRaw`.

## RawMasterData

`goodsId`, `deptId`, `grpId`, `grpName`, `mercName`, `productName`, `fullNameVi`, `mercType`, `itemType`, `openDate`, `modiDate`, `userId`, `wsId`, `costPrice`, `salePrice`, `lastImportPrice`, `prefPrice`, `wsPrice`, `basePrice`, `mdPrice`, `discRtpr`, `discSppr`, `minMargin`, `maxMargin`, `dms`, `unitConv`, `unitDesc`, `unitSymb`, `pieceUnit`, `rcQty`, `rcType`, `varType`, `varId`, `varDesc`, `isConsign`, `isOwnBrand`, `brandName`, `useByDays`, `co`, `isInstall`, `isPack`, `isSerial`, `isTicket`, `isReserv`, `hasExpiry`, `domestic`, `posShow`, `isWebShow`, `imageErp`, `ref`, `upcCode`, `pluCode`, `discCode`, `allRaw`.

## ContractDetails

`contractCode`, `companyName`, `supplierShortName`, `taxNumber`, `companyAddress`, `bankAccount`, `bankAccountOwner`, `bankName`, `buyerInCharge`, `accountantInCharge`, `mdInCharge`, `contractSignDate`, `contractExpireDate`, `contractRenewDate`, `paymentTerm`, `minOrderValue`, `targetRevenue`, `avgProfitMargin`, `deliveryCommitment`, `minExpiryOnDelivery`, `deliveryLeadTimeDays`, `returnPolicy`, `nearExpiryReturnDays`, `displayFeePercent`, `marketingFeePercent`, `newSkuCreationFee`, `discountTiers`, `legalRepName`, `legalRepPhone`, `legalRepEmail`, `contactName`, `contactPhone`, `contactEmail`, `allRawContract`.

## EcommerceRecord

`productId`, `lang`, `title`, `url`, `imageUrl`, `packaging`, `weightGrams`, `conversionRate`, `webPrice`, `expectedWebPrice`, `priceDiff`, `inventoryQuantity`, `publishedAt`, `bodyHtml`, `tags`, `productType`.

## StorePrice

`storeCode`, `storeName`, `shortName`, `isOverride`, `overrideType`, `retailPriceVat`, `netPrice`, `costPrice`, `grossProfit`, `marginPercent`, `diffFromBase`, `diffPercent`, `effectiveFrom`, `effectiveTo`, `posSyncStatus`, `notes`, `updatedAt`.

## ChannelPrice

`channelCode`, `channelName`, `platform`, `channelSku`, `isPublished`, `listedPrice`, `campaignPrice`, `effectivePrice`, `platformFeePercent`, `platformFeeAmount`, `packagingFee`, `netPayout`, `netProfit`, `netMarginPercent`, `diffFromBase`, `diffPercent`, `syncStatus`, `lastSyncedAt`, `updatedAt`, `ruleAppliedLevel`, `ruleAdjustmentType`, `ruleAdjustmentValue`, `ruleValidFrom`, `ruleValidTo`.

## Pricing

`skuId`, `skuCode`, `barcode`, `productName`, `unit`, `costPrice`, `basePriceVat`, `vatRate`, `baseMarginPercent`, `baseNetPrice`, `baseGrossProfit`, `stores`, `channels`, `hasStoreOverrides`, `publishedChannelsCount`, `baseSalePriceVat`, `baseNetSalePrice`, `vatCode`, `outTaxRate`, `outTaxCode`, `inTaxRate`, `inTaxCode`, `storeOverrides`.

## StoreOverride

`storeCode`, `storeName`, `salePriceVat`, `isOverride`, `diffPercent`.

## InventoryCttk

`totalStock`, `updatedAt`, `details`.

## InventoryDetail

`storeCode`, `storeName`, `shortName`, `maLo`, `maViTri`, `slTon`, `lastMovement`.

## Ecommerce

`isOnWebsite`, `conversionRate`, `primaryImageUrl`, `allImages`, `vietnamese`, `english`, `webRecordsCount`, `webTitle`, `webUrl`, `webPrice`, `imageUrl`, `packaging`.

## Bản ghi nguồn đầy đủ

| sourceRecords | Nguồn PostgreSQL |
|---|---|
| `skuDef[]` | `catalog.sku_def` |
| `barcodes[]` | `catalog.barcode` |
| `tthh[]` | `catalog.tthh` |
| `spprice[]` | `procurement.spprice` |
| `suppliers[]` | `procurement.supplier` |
| `supplierContracts[]` | `procurement.supplier_contract` |
| `storeMaster[]` | `catalog.stores` |
| `storePrices[]` | `pricing.pricing_store_prices` |
| `channelPrices[]` | `catalog.sku_channel_pricing` |
| `channelRules[]` | `pricing.pricing_channel_rules` |
| `contextOverrides[]` | `pricing.pricing_context_overrides` |
| `ecommerceProducts[]` | `growth_ecommerce.haravan_products` |
| `ecommerceVariants[]` | `growth_ecommerce.haravan_product_variants` |
| `cttk[]` | `accounting.cttk` |

Mỗi bản ghi gửi toàn bộ cột và toàn bộ raw_data, giữ tên trường nguồn. Chỉ loại hai cờ Fresh đã được chủ dự án yêu cầu bỏ. Không áp dụng bộ lọc startswith("_") cho các trường dữ liệu nguồn.

## Danh sách cột PostgreSQL đã đối chiếu

Đọc schema bằng READ ONLY lúc 2026-10-27 13:13:43.337756+07:00. Không xuất bản dữ liệu SKU thật.

### accounting.cttk

`_id`, `raw_data`, `migrated_at`, `ma_vt`, `ma_kho`, `ma_lo`, `ma_vi_tri`, `dt_tk1`, `dt_tk2`, `dt_tk3`, `sl_ton`, `sl_ton2`, `datetime2`, `source_db`.

### catalog.barcode

`_id`, `raw_data`, `migrated_at`, `barcode`, `sku_id`, `type`, `descript`, `unit_symb`, `unitconv`, `isdefault`, `last_updated`, `data_hash`, `source_db`.

### catalog.sku_channel_pricing

`id`, `sku_id`, `channel_code`, `channel_sku`, `listed_price`, `campaign_price`, `platform_fee_percent`, `packaging_fee`, `net_estimated_price`, `net_margin_percent`, `auto_markup_rule`, `sync_status`, `last_synced_at`, `is_published`, `created_by`, `updated_by`, `created_at`, `updated_at`.

### catalog.sku_def

`_id`, `raw_data`, `migrated_at`, `sku_id`, `sku_code`, `barcode`, `upc_code`, `ref`, `plu_code`, `dept_id`, `grp_id`, `grp_name`, `matrix`, `short_name`, `full_name`, `merc_name`, `goods_id`, `var_type`, `var_id`, `var_desc`, `unit_desc`, `piceunit`, `unit_symb`, `source_db`, `full_name_unicode`, `unaccented_name`, `conversion_rate`, `conversion_rate_updated_at`, `conversion_rate_updated_by`.

### catalog.stores

`store_code`, `legacy_code`, `store_name`, `short_name`, `store_type`, `address`, `ward`, `district`, `city`, `phone`, `email`, `manager_name`, `manager_phone`, `haravan_location_id`, `pos_branch_id`, `is_active`, `display_order`, `notes`, `created_at`, `updated_at`.

### catalog.tthh

`_id`, `raw_data`, `migrated_at`, `ma_san_pham`, `ten_sp`, `nguyen_nhan_dong_mo_trang_thai`, `ngay_doi_trang_thai_gan_nhat`, `tt_gom`, `hsd`, `hsd_quy_doi_theo_quy_tac`, `md_phu_trach`, `thoi_gian_thay_doi_nguoi_dat`, `buyer_phu_trach`, `so_luong_trung_bay_toi_thieu_10001`, `so_luong_trung_bay_toi_thieu_10004`, `so_luong_trung_bay_toi_thieu_10005`, `so_luong_trung_bay_toi_thieu_10006`, `hstmax`, `hstmin`, `piceunit`, `san_pham_khong_co_dau_vao`, `unnamed_2`, `unnamed_3`, `source_db`.

### growth_ecommerce.haravan_product_variants

`id`, `product_id`, `sku`, `barcode`, `title`, `price`, `compare_at_price`, `grams`, `weight`, `weight_unit`, `inventory_quantity`, `raw_data`, `synced_at`, `packaging`, `conversion_rate`.

### growth_ecommerce.haravan_products

`id`, `title`, `handle`, `vendor`, `product_type`, `published_at`, `is_published`, `primary_image_url`, `images`, `body_html`, `tags`, `raw_data`, `updated_at_haravan`, `synced_at`, `lang`.

### pricing.pricing_channel_rules

`channel_code`, `channel_name`, `is_dynamic`, `default_markup_percent`, `packaging_fee_vnd`, `rounding_rule`, `restrict_to_priced_items`, `is_virtual_channel`, `is_active`, `updated_at`.

### pricing.pricing_context_overrides

`id`, `sku`, `branch_code`, `channel_code`, `override_price_vnd`, `min_price_vnd`, `is_excluded`, `valid_from`, `valid_to`, `proposal_id`, `created_by`, `is_active`, `created_at`.

### pricing.pricing_store_prices

`id`, `sku_id`, `store_code`, `retail_price_vat`, `diff_percent`, `is_active`, `created_at`.

### procurement.spprice

`_id`, `raw_data`, `migrated_at`, `supp_id`, `sku_id`, `stk_id`, `zone_code`, `merc_id`, `unit_symb`, `base_unit`, `unitconv`, `spprice`, `lastsppr`, `surp_pc`, `minprice`, `maxprice`, `modify`, `out_discpc`, `out_cmpc`, `vat_incl`, `tax_code`, `isdefault`, `open_date`, `source_db`.

### procurement.supplier

`_id`, `raw_data`, `migrated_at`, `supp_id`, `supp_code`, `type`, `node_id`, `supp_name`, `grp_id`, `account_id`, `acc_cys`, `con_person`, `tax_name`, `tax_addr`, `tax_id`, `address`, `address2`, `country`, `plc_id`, `phone`, `fax`, `mobi`, `mobi2`, `source_db`.

### procurement.supplier_contract

`_id`, `raw_data`, `migrated_at`, `thumuaquanlyncc`, `stt`, `mahopdong`, `mancc`, `tennccrutgon`, `tencongty`, `diachicongty`, `masothue`, `sotaikhoan`, `chutaikhoan`, `tennganhang`, `diachinganhang`, `nguoidaidienphapluat_ten`, `nguoidaidienphapluat_sdt`, `nguoidaidienphapluat_email`, `nguoilienhe_ten`, `nguoilienhe_sdt`, `nguoilienhe_email`, `nhomhang`, `trangthaihoatdong`, `source_db`.

