import catalog from '../../../src/anybench/brand_assets/catalog.json';

export type BrandIdentity = { label: string; icon: string };
export type Branding = { harness: BrandIdentity; providers: BrandIdentity[] };
export const harnessBrand = (harness: string): BrandIdentity =>
  (catalog.harnesses as Record<string, BrandIdentity>)[harness] || { label: harness || 'Unknown harness', icon: 'generic' };

export function Brand({ brand, kind }: { brand: BrandIdentity; kind?: string }) {
  const src = (catalog.icons as Record<string, string>)[brand.icon] || catalog.icons.generic;
  return <span className="brand-chip" title={kind ? `${kind}: ${brand.label}` : brand.label}>
    <span className="logo-tile"><img src={src} alt="" width="18" height="18" /></span>
    {kind && <span className="sr-only">{kind}: </span>}{brand.label}
  </span>;
}
export function BrandRow({ branding, harness }: { branding?: Branding; harness: string }) {
  return <div className="brand-row"><Brand brand={branding?.harness || harnessBrand(harness)} kind="Harness" />
    {(branding?.providers || [{ label: 'Provider unavailable', icon: 'generic' }]).map(brand => <Brand key={brand.label} brand={brand} kind="Provider" />)}
  </div>;
}
