import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <div className="mx-auto mt-24 max-w-md text-center">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="mt-2 text-sm text-slate-500">The page you requested does not exist.</p>
      <Link to="/" className="mt-4 inline-block text-sm font-medium text-blue-900 underline">
        Go to the home page
      </Link>
    </div>
  );
}
